"""
JARVIS Database Module
- SQLite con WAL mode per concorrenza
- Gestione utenti con ruoli (admin/user)
- Autenticazione con Passkeys + Email fallback
- Tracking speaker per audit e memoria
- Sistema di pesi per memoria conversazionale
"""

import sqlite3
import time
import json
import hashlib
import re
import secrets
import logging
import requests
from pathlib import Path
from typing import Any, Optional, List, Dict
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("JARVIS_DB")

# Path del database
DB_PATH = Path("/app/data/jarvis_state.db")


class UserRole(Enum):
    ADMIN = "admin"
    USER = "user"
    GUEST = "guest"


@dataclass
class User:
    id: int
    name: str
    role: UserRole
    voice_enrolled: bool
    voice_model_path: Optional[str]
    created_at: float
    # Campi auth (opzionali per backward compatibility)
    email: Optional[str] = None
    password_hash: Optional[str] = None
    email_verified: bool = False
    # Campi Telegram
    telegram_id: Optional[int] = None
    telegram_username: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    @property
    def has_password(self) -> bool:
        return self.password_hash is not None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role.value,
            "voice_enrolled": self.voice_enrolled,
            "is_admin": self.is_admin,
            "email": self.email,
            "email_verified": self.email_verified,
            "has_password": self.has_password,
            "telegram_id": self.telegram_id,
            "telegram_username": self.telegram_username
        }


@dataclass
class Session:
    id: str
    user_id: int
    created_at: float
    expires_at: float
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return time.time() < self.expires_at


@dataclass
class PasskeyCredential:
    id: str
    user_id: int
    public_key: bytes
    credential_id: bytes
    sign_count: int
    device_name: str
    created_at: float
    last_used: Optional[float] = None


# ===========================================================================
# CONNECTION MANAGEMENT (con WAL mode)
# ===========================================================================

def _get_conn() -> sqlite3.Connection:
    """
    Restituisce una connessione con WAL mode abilitato.
    WAL (Write-Ahead Logging) permette letture concorrenti durante le scritture.
    """
    import config as _cfg
    conn = sqlite3.connect(DB_PATH, timeout=_cfg.TIMEOUTS["sqlite"])
    conn.row_factory = sqlite3.Row  # Accesso per nome colonna

    # Abilita WAL mode (una volta sola, persiste)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_cfg.TIMEOUTS['sqlite'] * 1000}")
    conn.execute("PRAGMA synchronous=NORMAL")  # Buon compromesso performance/sicurezza

    return conn


def init_db():
    """Inizializza tutte le tabelle del database."""
    conn = _get_conn()
    c = conn.cursor()
    
    # 1. UTENTI (con campi autenticazione e Telegram)
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE,
        password_hash TEXT,
        email_verified BOOLEAN DEFAULT FALSE,
        role TEXT DEFAULT 'user',
        voice_enrolled BOOLEAN DEFAULT FALSE,
        voice_model_path TEXT,
        telegram_id INTEGER UNIQUE,
        telegram_username TEXT,
        created_at REAL DEFAULT (strftime('%s', 'now'))
    )''')

    # Migration: aggiungi colonne auth e telegram se non esistono (per DB esistenti)
    for col, col_type in [
        ("email", "TEXT"),
        ("password_hash", "TEXT"),
        ("email_verified", "BOOLEAN DEFAULT FALSE"),
        ("telegram_id", "INTEGER UNIQUE"),
        ("telegram_username", "TEXT")
    ]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # Colonna già esiste
    
    # 2. AZIONI IN ATTESA (Approvazioni)
    c.execute('''CREATE TABLE IF NOT EXISTS pending_actions (
        action_id TEXT PRIMARY KEY,
        payload TEXT,
        requested_by INTEGER REFERENCES users(id),
        timestamp REAL
    )''')
    
    # 3. STATO SISTEMA
    c.execute('''CREATE TABLE IF NOT EXISTS system_state (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    
    # 4. AUDIT LOG (con speaker tracking)
    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        category TEXT,
        message TEXT,
        speaker_id INTEGER REFERENCES users(id),
        speaker_name TEXT
    )''')
    
    # 5. MEMORIA CONVERSAZIONALE (con speaker tracking)
    c.execute('''CREATE TABLE IF NOT EXISTS chat_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        role TEXT,
        content TEXT,
        source TEXT,
        speaker_id INTEGER REFERENCES users(id),
        speaker_name TEXT
    )''')
    
    # 6. TELEGRAM STREAMING
    c.execute('''CREATE TABLE IF NOT EXISTS telegram_streams (
        source_id TEXT PRIMARY KEY,
        message_id INTEGER,
        last_update REAL
    )''')
    
    # 7. PREFERENZE UTENTE (per-user)
    c.execute('''CREATE TABLE IF NOT EXISTS user_preferences (
        user_id INTEGER REFERENCES users(id),
        pref_key TEXT,
        pref_value TEXT,
        PRIMARY KEY (user_id, pref_key)
    )''')
    
    # 8. PREFERENZE GLOBALI
    c.execute('''CREATE TABLE IF NOT EXISTS global_preferences (
        pref_key TEXT PRIMARY KEY,
        pref_value TEXT
    )''')
    
    # 9. DEVICE FAILURES
    c.execute('''CREATE TABLE IF NOT EXISTS device_failures (
        entity_id TEXT PRIMARY KEY,
        count INTEGER,
        last_error TEXT,
        timestamp REAL
    )''')
    
    # 10. QUERY CACHE
    c.execute('''CREATE TABLE IF NOT EXISTS query_cache (
        query_hash TEXT PRIMARY KEY,
        query_text TEXT,
        response TEXT,
        hit_count INTEGER DEFAULT 1,
        last_used REAL,
        auto_learned BOOLEAN DEFAULT FALSE
    )''')
    
    # 11. QUERY FREQUENCY (per auto-learning)
    c.execute('''CREATE TABLE IF NOT EXISTS query_frequency (
        query_normalized TEXT PRIMARY KEY,
        count INTEGER DEFAULT 1,
        last_response TEXT,
        response_consistent BOOLEAN DEFAULT TRUE
    )''')

    # 12. LOCATIONS (Multi-Home Assistant)
    c.execute('''CREATE TABLE IF NOT EXISTS locations (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        city TEXT,
        hass_url TEXT NOT NULL,
        hass_token TEXT NOT NULL,
        entity_map_path TEXT,
        has_security BOOLEAN DEFAULT FALSE,
        enabled BOOLEAN DEFAULT TRUE,
        keywords TEXT,
        created_at REAL DEFAULT (strftime('%s', 'now'))
    )''')

    # Migration: aggiungi colonna keywords se non esiste (per DB esistenti)
    try:
        c.execute("ALTER TABLE locations ADD COLUMN keywords TEXT")
    except sqlite3.OperationalError:
        pass  # Colonna già esiste

    # 13. USER LOCATIONS (Location corrente per utente)
    c.execute('''CREATE TABLE IF NOT EXISTS user_locations (
        user_id INTEGER PRIMARY KEY REFERENCES users(id),
        location_id TEXT REFERENCES locations(id),
        source TEXT,
        updated_at REAL
    )''')

    # 14. ENTITY MAPS (Struttura stanze/dispositivi per location)
    # entity_name = friendly name (per umani e LLM)
    # entity_id = ID reale Home Assistant (per chiamate API)
    c.execute('''CREATE TABLE IF NOT EXISTS entity_maps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        location_id TEXT NOT NULL REFERENCES locations(id),
        zone TEXT NOT NULL,
        area TEXT NOT NULL,
        room TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_name TEXT NOT NULL,
        entity_id TEXT,
        created_at REAL DEFAULT (strftime('%s', 'now')),
        UNIQUE(location_id, zone, area, room, entity_type, entity_name)
    )''')

    # Migration: aggiungi colonna entity_id se non esiste (per DB esistenti)
    try:
        c.execute("ALTER TABLE entity_maps ADD COLUMN entity_id TEXT")
    except sqlite3.OperationalError:
        pass  # Colonna già esistente

    # Migration: aggiungi colonna device_name se non esiste
    try:
        c.execute("ALTER TABLE entity_maps ADD COLUMN device_name TEXT")
    except sqlite3.OperationalError:
        pass  # Colonna già esistente

    # 15. SESSIONS (Autenticazione web)
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at REAL DEFAULT (strftime('%s', 'now')),
        expires_at REAL NOT NULL,
        ip_address TEXT,
        user_agent TEXT
    )''')

    # 16. PASSKEY CREDENTIALS (WebAuthn)
    c.execute('''CREATE TABLE IF NOT EXISTS passkey_credentials (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        credential_id BLOB NOT NULL UNIQUE,
        public_key BLOB NOT NULL,
        sign_count INTEGER DEFAULT 0,
        device_name TEXT,
        created_at REAL DEFAULT (strftime('%s', 'now')),
        last_used REAL
    )''')

    # 17. OTP TOKENS (Email verification / Password reset)
    c.execute('''CREATE TABLE IF NOT EXISTS otp_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        email TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        purpose TEXT NOT NULL,
        expires_at REAL NOT NULL,
        used BOOLEAN DEFAULT FALSE,
        created_at REAL DEFAULT (strftime('%s', 'now'))
    )''')

    # 18. VOICE DEVICES (configurazione AtomS3R)
    c.execute('''CREATE TABLE IF NOT EXISTS voice_devices (
        device_id TEXT PRIMARY KEY,
        friendly_name TEXT,
        location_id TEXT REFERENCES locations(id),
        output_speaker TEXT NOT NULL,
        fallback_speaker TEXT,
        fallback_telegram BOOLEAN DEFAULT 1,
        fallback_local_speaker BOOLEAN DEFAULT 1,
        enabled BOOLEAN DEFAULT 1,
        created_at REAL DEFAULT (strftime('%s', 'now')),
        last_seen_at REAL,
        firmware_version TEXT,
        ip_address TEXT,
        notes TEXT
    )''')

    # 19. USER MEMORY HOURLY (warm layer - summaries orari per utente)
    c.execute('''CREATE TABLE IF NOT EXISTS user_memory_hourly (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        hour_start REAL NOT NULL,
        hour_end REAL NOT NULL,
        summary TEXT NOT NULL,
        message_count INTEGER DEFAULT 0,
        created_at REAL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    # 20. USER MEMORY DAILY (cold layer - summaries giornalieri per utente)
    c.execute('''CREATE TABLE IF NOT EXISTS user_memory_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        summary TEXT NOT NULL,
        patterns_json TEXT,
        anomalies_json TEXT,
        created_at REAL DEFAULT (strftime('%s', 'now')),
        FOREIGN KEY (user_id) REFERENCES users(id),
        UNIQUE(user_id, date)
    )''')

    # 21. USER MEMORY LONGTERM (fatti permanenti per utente)
    c.execute('''CREATE TABLE IF NOT EXISTS user_memory_longterm (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        fact TEXT NOT NULL,
        category TEXT,
        confidence REAL DEFAULT 0.8,
        source TEXT,
        created_at REAL DEFAULT (strftime('%s', 'now')),
        updated_at REAL,
        expires_at REAL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    # INDICI per performance
    c.execute('CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_audit_speaker ON audit_log(speaker_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_chat_timestamp ON chat_memory(timestamp DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_chat_speaker ON chat_memory(speaker_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_locations_enabled ON locations(enabled)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_locations_user ON user_locations(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_entity_maps_location ON entity_maps(location_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_entity_maps_room ON entity_maps(location_id, room)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_entity_maps_entity_id ON entity_maps(location_id, entity_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_entity_maps_name ON entity_maps(location_id, entity_name)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_passkeys_user ON passkey_credentials(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_otp_email ON otp_tokens(email)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_otp_expires ON otp_tokens(expires_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_voice_devices_location ON voice_devices(location_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_voice_devices_enabled ON voice_devices(enabled)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_voice_devices_last_seen ON voice_devices(last_seen_at)')

    # Indici per user memory
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_hourly_user_time ON user_memory_hourly(user_id, hour_start)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_longterm_user ON user_memory_longterm(user_id, category)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_daily_user ON user_memory_daily(user_id, date)')

    # 22. ACCESS LOG (logging HTTP completo per sicurezza)
    c.execute('''CREATE TABLE IF NOT EXISTS access_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        status_code INTEGER,
        ip_address TEXT,
        user_agent TEXT,
        user_id INTEGER,
        response_time_ms REAL
    )''')

    # 23. AUTH ATTEMPTS (tracking tentativi di autenticazione)
    c.execute('''CREATE TABLE IF NOT EXISTS auth_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        email TEXT,
        ip_address TEXT,
        user_agent TEXT,
        success BOOLEAN NOT NULL DEFAULT FALSE,
        failure_reason TEXT
    )''')

    # Indici access_log
    c.execute('CREATE INDEX IF NOT EXISTS idx_access_log_timestamp ON access_log(timestamp DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_access_log_ip ON access_log(ip_address)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_access_log_path ON access_log(path)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_access_log_status ON access_log(status_code)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_access_log_user ON access_log(user_id)')

    # Indici auth_attempts
    c.execute('CREATE INDEX IF NOT EXISTS idx_auth_attempts_email ON auth_attempts(email, timestamp DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_auth_attempts_ip ON auth_attempts(ip_address, timestamp DESC)')

    # NOTA: Nessun utente di default - il wizard di primo avvio crea l'admin
    # NOTA: Non inseriamo locations di default - il sistema funziona in modalità AI-only
    # finché l'utente non configura le proprie istanze Home Assistant dalla dashboard

    conn.commit()
    conn.close()


# ===========================================================================
# USER MANAGEMENT
# ===========================================================================

def create_user(name: str, role: str = "user") -> Optional[int]:
    """Crea un nuovo utente. Ritorna l'ID o None se esiste già."""
    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (name, role) VALUES (?, ?)", (name, role))
        conn.commit()
        user_id = c.lastrowid
        conn.close()
        return user_id
    except sqlite3.IntegrityError:
        conn.close()
        return None


def _row_to_user(row) -> User:
    """Helper per costruire User da sqlite row."""
    keys = row.keys()
    return User(
        id=row['id'],
        name=row['name'],
        role=UserRole(row['role']),
        voice_enrolled=bool(row['voice_enrolled']),
        voice_model_path=row['voice_model_path'],
        created_at=row['created_at'],
        email=row['email'] if 'email' in keys else None,
        password_hash=row['password_hash'] if 'password_hash' in keys else None,
        email_verified=bool(row['email_verified']) if 'email_verified' in keys else False,
        telegram_id=row['telegram_id'] if 'telegram_id' in keys else None,
        telegram_username=row['telegram_username'] if 'telegram_username' in keys else None
    )


def get_user_by_name(name: str) -> Optional[User]:
    """Recupera un utente per nome."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE LOWER(name) = LOWER(?)", (name,))
    row = c.fetchone()
    conn.close()
    return _row_to_user(row) if row else None


def get_user_by_id(user_id: int) -> Optional[User]:
    """Recupera un utente per ID."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return _row_to_user(row) if row else None


def get_user_by_email(email: str) -> Optional[User]:
    """Recupera un utente per email."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,))
    row = c.fetchone()
    conn.close()
    return _row_to_user(row) if row else None


def get_user_by_telegram_id(telegram_id: int) -> Optional[User]:
    """Recupera un utente per telegram_id."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = c.fetchone()
    conn.close()
    return _row_to_user(row) if row else None


def link_telegram_to_user(user_id: int, telegram_id: int, username: str = None) -> bool:
    """Collega account Telegram a utente esistente."""
    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute(
            "UPDATE users SET telegram_id = ?, telegram_username = ? WHERE id = ?",
            (telegram_id, username, user_id)
        )
        conn.commit()
        affected = c.rowcount
        conn.close()
        return affected > 0
    except sqlite3.IntegrityError:
        conn.close()
        return False  # telegram_id già associato ad altro utente


def unlink_telegram_from_user(user_id: int) -> bool:
    """Scollega account Telegram da utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET telegram_id = NULL, telegram_username = NULL WHERE id = ?",
        (user_id,)
    )
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def is_telegram_authorized(telegram_id: int) -> bool:
    """Check veloce se telegram_id è autorizzato."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,))
    row = c.fetchone()
    conn.close()
    return row is not None


def get_all_users() -> List[User]:
    """Recupera tutti gli utenti."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY name")
    rows = c.fetchall()
    conn.close()
    return [_row_to_user(row) for row in rows]


def update_user_role(user_id: int, role: str) -> bool:
    """Aggiorna il ruolo di un utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def update_user_voice_enrollment(user_id: int, enrolled: bool, model_path: Optional[str] = None) -> bool:
    """Aggiorna lo stato di enrollment vocale."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET voice_enrolled = ?, voice_model_path = ? WHERE id = ?",
        (enrolled, model_path, user_id)
    )
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def delete_user(user_id: int) -> bool:
    """Elimina un utente (non elimina i log per audit trail)."""
    conn = _get_conn()
    c = conn.cursor()
    # Non eliminare admin
    c.execute("SELECT role FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    if row and row['role'] == 'admin':
        # Conta admin
        c.execute("SELECT COUNT(*) as cnt FROM users WHERE role = 'admin'")
        if c.fetchone()['cnt'] <= 1:
            conn.close()
            return False  # Non eliminare l'ultimo admin

    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


# ===========================================================================
# AUTHENTICATION
# ===========================================================================

def hash_password(password: str) -> str:
    """Hash password con salt usando SHA256."""
    salt = secrets.token_hex(16)
    pwd_hash = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{pwd_hash}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verifica password contro hash."""
    if not password_hash or ':' not in password_hash:
        return False
    salt, stored_hash = password_hash.split(':', 1)
    pwd_hash = hashlib.sha256((salt + password).encode()).hexdigest()
    return secrets.compare_digest(pwd_hash, stored_hash)


def create_user_with_auth(
    name: str,
    email: str,
    password: Optional[str] = None,
    role: str = "user"
) -> Optional[int]:
    """Crea un nuovo utente con credenziali auth."""
    conn = _get_conn()
    c = conn.cursor()
    try:
        password_hash = hash_password(password) if password else None
        c.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            (name, email.lower(), password_hash, role)
        )
        conn.commit()
        user_id = c.lastrowid
        conn.close()
        return user_id
    except sqlite3.IntegrityError:
        conn.close()
        return None


def update_user_password(user_id: int, password: str) -> bool:
    """Aggiorna la password di un utente."""
    conn = _get_conn()
    c = conn.cursor()
    password_hash = hash_password(password)
    c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def update_user_email(user_id: int, email: str, verified: bool = False) -> bool:
    """Aggiorna l'email di un utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET email = ?, email_verified = ? WHERE id = ?",
        (email.lower(), verified, user_id)
    )
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def set_email_verified(user_id: int, verified: bool = True) -> bool:
    """Imposta lo stato di verifica email."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET email_verified = ? WHERE id = ?", (verified, user_id))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def has_any_admin() -> bool:
    """Controlla se esiste almeno un admin nel sistema."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM users WHERE role = 'admin'")
    row = c.fetchone()
    conn.close()
    return row['cnt'] > 0 if row else False


# ===========================================================================
# SESSIONS
# ===========================================================================

def create_session(
    user_id: int,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    duration: int = None
) -> str:
    """Crea una nuova sessione. Ritorna il session_id."""
    import config as _cfg
    if duration is None:
        duration = _cfg.SESSION_DURATION_SECONDS
    session_id = secrets.token_urlsafe(32)
    conn = _get_conn()
    c = conn.cursor()
    now = time.time()
    c.execute(
        "INSERT INTO sessions (id, user_id, created_at, expires_at, ip_address, user_agent) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, user_id, now, now + duration, ip_address, user_agent)
    )
    conn.commit()
    conn.close()
    return session_id


def get_session(session_id: str) -> Optional[Session]:
    """Recupera una sessione valida."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM sessions WHERE id = ? AND expires_at > ?", (session_id, time.time()))
    row = c.fetchone()
    conn.close()
    if row:
        return Session(
            id=row['id'],
            user_id=row['user_id'],
            created_at=row['created_at'],
            expires_at=row['expires_at'],
            ip_address=row['ip_address'],
            user_agent=row['user_agent']
        )
    return None


def delete_session(session_id: str) -> bool:
    """Elimina una sessione (logout)."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def delete_user_sessions(user_id: int) -> int:
    """Elimina tutte le sessioni di un utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected


def cleanup_expired_sessions() -> int:
    """Rimuove sessioni scadute."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected


def get_user_sessions(user_id: int) -> List[Session]:
    """Recupera tutte le sessioni attive di un utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM sessions WHERE user_id = ? AND expires_at > ? ORDER BY created_at DESC",
        (user_id, time.time())
    )
    rows = c.fetchall()
    conn.close()
    return [Session(
        id=row['id'],
        user_id=row['user_id'],
        created_at=row['created_at'],
        expires_at=row['expires_at'],
        ip_address=row['ip_address'],
        user_agent=row['user_agent']
    ) for row in rows]


# ===========================================================================
# PASSKEY CREDENTIALS (WebAuthn)
# ===========================================================================

def save_passkey_credential(
    user_id: int,
    credential_id: bytes,
    public_key: bytes,
    device_name: str
) -> str:
    """Salva una nuova passkey. Ritorna l'ID."""
    pk_id = secrets.token_urlsafe(16)
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO passkey_credentials (id, user_id, credential_id, public_key, device_name) VALUES (?, ?, ?, ?, ?)",
        (pk_id, user_id, credential_id, public_key, device_name)
    )
    conn.commit()
    conn.close()
    return pk_id


def get_passkey_by_credential_id(credential_id: bytes) -> Optional[PasskeyCredential]:
    """Recupera passkey per credential_id."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM passkey_credentials WHERE credential_id = ?", (credential_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return PasskeyCredential(
            id=row['id'],
            user_id=row['user_id'],
            public_key=row['public_key'],
            credential_id=row['credential_id'],
            sign_count=row['sign_count'],
            device_name=row['device_name'],
            created_at=row['created_at'],
            last_used=row['last_used']
        )
    return None


def get_user_passkeys(user_id: int) -> List[PasskeyCredential]:
    """Recupera tutte le passkey di un utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM passkey_credentials WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [PasskeyCredential(
        id=row['id'],
        user_id=row['user_id'],
        public_key=row['public_key'],
        credential_id=row['credential_id'],
        sign_count=row['sign_count'],
        device_name=row['device_name'],
        created_at=row['created_at'],
        last_used=row['last_used']
    ) for row in rows]


def update_passkey_sign_count(credential_id: bytes, new_count: int) -> bool:
    """Aggiorna sign_count e last_used dopo autenticazione."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE passkey_credentials SET sign_count = ?, last_used = ? WHERE credential_id = ?",
        (new_count, time.time(), credential_id)
    )
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def delete_passkey(pk_id: str, user_id: int) -> bool:
    """Elimina una passkey (solo se appartiene all'utente)."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM passkey_credentials WHERE id = ? AND user_id = ?", (pk_id, user_id))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


# ===========================================================================
# OTP TOKENS (Email verification / Password reset)
# ===========================================================================

import config as _cfg_otp
OTP_EXPIRY = _cfg_otp.OTP_EXPIRY_SECONDS


def create_otp_token(email: str, purpose: str, user_id: Optional[int] = None) -> str:
    """Crea un OTP token. Ritorna il token in chiaro (da inviare via email)."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = _get_conn()
    c = conn.cursor()
    # Invalida token precedenti stesso scopo
    c.execute(
        "UPDATE otp_tokens SET used = TRUE WHERE email = ? AND purpose = ? AND used = FALSE",
        (email.lower(), purpose)
    )
    c.execute(
        "INSERT INTO otp_tokens (user_id, email, token_hash, purpose, expires_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, email.lower(), token_hash, purpose, time.time() + OTP_EXPIRY)
    )
    conn.commit()
    conn.close()
    return token


def verify_otp_token(token: str, email: str, purpose: str) -> Optional[int]:
    """Verifica un OTP token. Ritorna user_id se valido, None altrimenti."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT user_id, id FROM otp_tokens
           WHERE token_hash = ? AND LOWER(email) = LOWER(?) AND purpose = ?
           AND expires_at > ? AND used = FALSE""",
        (token_hash, email, purpose, time.time())
    )
    row = c.fetchone()
    if row:
        # Marca come usato
        c.execute("UPDATE otp_tokens SET used = TRUE WHERE id = ?", (row['id'],))
        conn.commit()
        conn.close()
        return row['user_id']
    conn.close()
    return None


def cleanup_expired_otp() -> int:
    """Rimuove OTP scaduti."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM otp_tokens WHERE expires_at < ?", (time.time(),))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected


# ===========================================================================
# SETUP STATUS
# ===========================================================================

def needs_initial_setup() -> bool:
    """Controlla se il sistema richiede il setup iniziale."""
    return not has_any_admin()


def _verify_ha_connections() -> bool:
    """Verifica se almeno una location HA configurata è raggiungibile (sync)."""
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT hass_url, hass_token FROM locations WHERE enabled = TRUE AND hass_url != '' AND hass_token != ''")
        rows = c.fetchall()
        conn.close()

        for row in rows:
            try:
                resp = requests.get(
                    f"{row['hass_url']}/api/",
                    headers={"Authorization": f"Bearer {row['hass_token']}"},
                    timeout=3
                )
                if resp.status_code == 200:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _verify_telegram_config() -> Dict[str, Any]:
    """Verifica configurazione Telegram: bot token valido e utenti collegati (sync)."""
    import config as _cfg_tg

    result = {
        "bot_configured": False,
        "users_linked": False,
    }

    # Verifica bot token
    token = _cfg_tg.JARVIS_APPROVAL_BOT_TOKEN
    if token:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=3
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    result["bot_configured"] = True
        except Exception:
            pass

    # Verifica utenti collegati
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM users WHERE telegram_id IS NOT NULL")
        result["users_linked"] = c.fetchone()['cnt'] > 0
        conn.close()
    except Exception:
        pass

    return result


def get_setup_status() -> Dict[str, Any]:
    """Ritorna lo stato completo del setup per la dashboard con progress tracking."""
    conn = _get_conn()
    c = conn.cursor()

    # ==== CORE ====
    c.execute("SELECT COUNT(*) as cnt FROM users WHERE role = 'admin'")
    has_admin = c.fetchone()['cnt'] > 0

    c.execute("SELECT COUNT(*) as cnt FROM users")
    total_users = c.fetchone()['cnt']

    c.execute("SELECT COUNT(*) as cnt FROM users WHERE email IS NOT NULL AND email != ''")
    users_with_email = c.fetchone()['cnt']

    c.execute("SELECT COUNT(*) as cnt FROM users WHERE password_hash IS NOT NULL")
    users_with_password = c.fetchone()['cnt']

    # ==== HOME ASSISTANT ====
    c.execute("SELECT COUNT(*) as cnt FROM locations WHERE enabled = TRUE")
    locations_count = c.fetchone()['cnt']

    c.execute("SELECT COUNT(*) as cnt FROM locations WHERE enabled = TRUE AND hass_url != '' AND hass_token != ''")
    locations_configured = c.fetchone()['cnt']

    c.execute("SELECT COUNT(DISTINCT location_id) as cnt FROM entity_maps")
    locations_with_entities = c.fetchone()['cnt']

    c.execute("SELECT COUNT(*) as cnt FROM entity_maps")
    entity_maps_count = c.fetchone()['cnt']

    # ==== SECURITY ====
    c.execute("SELECT COUNT(*) as cnt FROM locations WHERE has_security = TRUE AND enabled = TRUE")
    locations_with_security = c.fetchone()['cnt']

    c.execute("""SELECT COUNT(*) as cnt FROM entity_maps
                 WHERE entity_type = 'camera' AND LOWER(zone) LIKE '%esterno%' OR LOWER(zone) LIKE '%perimetral%'""")
    perimetral_cameras = c.fetchone()['cnt']

    c.execute("""SELECT COUNT(*) as cnt FROM entity_maps
                 WHERE entity_type = 'camera' AND (LOWER(zone) LIKE '%interno%' OR LOWER(zone) LIKE '%internal%')""")
    internal_cameras = c.fetchone()['cnt']

    # ==== VOICE RECOGNITION ====
    c.execute("SELECT COUNT(*) as cnt FROM users WHERE voice_enrolled = TRUE")
    voice_enrolled_count = c.fetchone()['cnt']

    # ==== PASSKEYS ====
    c.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM passkey_credentials")
    users_with_passkeys = c.fetchone()['cnt']

    conn.close()

    # Calculate progress percentages
    def calc_progress(items: list) -> int:
        """Calcola percentuale basata su lista di bool."""
        if not items:
            return 0
        return int((sum(1 for x in items if x) / len(items)) * 100)

    core_items = [has_admin, users_with_email > 0]
    ha_items = [locations_count > 0, locations_with_entities > 0] if locations_count > 0 else []
    security_items = [locations_with_security > 0, perimetral_cameras > 0, internal_cameras > 0] if locations_with_security > 0 else []
    voice_items = [voice_enrolled_count > 0] if total_users > 0 else []

    # Overall progress (core required, others optional)
    all_required = [has_admin]
    all_optional = [
        locations_count > 0,
        locations_with_entities > 0,
        voice_enrolled_count > 0
    ]

    required_progress = calc_progress(all_required)
    optional_complete = sum(1 for x in all_optional if x)
    total_progress = int((required_progress * 0.5) + (optional_complete / max(len(all_optional), 1) * 50))

    # ==== TELEGRAM VERIFICATION ====
    tg_status = _verify_telegram_config()
    tg_items = [tg_status["bot_configured"], tg_status["users_linked"]]

    return {
        "needs_setup": not has_admin,
        "total_progress": total_progress,

        "core": {
            "progress": calc_progress(core_items),
            "items": {
                "has_admin": has_admin,
                "users_with_auth": users_with_password > 0
            },
            "stats": {
                "total_users": total_users,
                "users_with_email": users_with_email,
                "users_with_passkeys": users_with_passkeys
            }
        },

        "home_assistant": {
            "progress": calc_progress(ha_items) if locations_count > 0 else 0,
            "enabled": locations_count > 0,
            "items": {
                "locations_configured": locations_configured > 0,
                "entity_maps_imported": locations_with_entities > 0,
                "connection_verified": _verify_ha_connections() if locations_configured > 0 else False
            },
            "stats": {
                "locations_count": locations_count,
                "locations_with_entities": locations_with_entities,
                "total_entities": entity_maps_count
            }
        },

        "security": {
            "progress": calc_progress(security_items) if locations_with_security > 0 else 0,
            "enabled": locations_with_security > 0,
            "items": {
                "location_marked": locations_with_security > 0,
                "cameras_perimetral": perimetral_cameras > 0,
                "cameras_internal": internal_cameras > 0
            },
            "stats": {
                "perimetral_cameras": perimetral_cameras,
                "internal_cameras": internal_cameras
            }
        },

        "voice_recognition": {
            "progress": calc_progress(voice_items) if total_users > 0 else 0,
            "enabled": voice_enrolled_count > 0,
            "items": {
                "profiles_enrolled": voice_enrolled_count > 0
            },
            "stats": {
                "enrolled_users": voice_enrolled_count,
                "total_users": total_users
            }
        },

        "telegram": {
            "progress": calc_progress(tg_items),
            "enabled": tg_status["bot_configured"],
            "items": {
                "bot_configured": tg_status["bot_configured"],
                "users_linked": tg_status["users_linked"]
            }
        }
    }


# ===========================================================================
# AUDIT LOG (con speaker segregation)
# ===========================================================================

def log_event(category: str, message: str, speaker_id: Optional[int] = None, speaker_name: Optional[str] = None):
    """Scrive un evento nel registro storico e pubblica su event bus."""
    conn = _get_conn()
    c = conn.cursor()
    ts = time.time()
    c.execute(
        "INSERT INTO audit_log (timestamp, category, message, speaker_id, speaker_name) VALUES (?, ?, ?, ?, ?)",
        (ts, category, message, speaker_id, speaker_name)
    )
    conn.commit()
    conn.close()

    # Pubblica su event bus (fire-and-forget)
    try:
        from event_bus import publish_sync
        publish_sync("audit_event", {
            "category": category,
            "message": message,
            "speaker_name": speaker_name,
            "timestamp": ts
        })
    except Exception:
        pass  # Non bloccare mai per errori event bus


def get_audit_summary(
    limit: int = 100,
    speaker_id: Optional[int] = None,
    is_admin: bool = False,
    categories: Optional[List[str]] = None
) -> List[Dict]:
    """
    Recupera gli eventi dal log.
    - Se is_admin=True: vede tutto
    - Se is_admin=False: vede solo i propri eventi
    """
    conn = _get_conn()
    c = conn.cursor()
    
    query = "SELECT timestamp, category, message, speaker_name FROM audit_log"
    params = []
    conditions = []
    
    # Filtro per speaker (non-admin vede solo i propri)
    if not is_admin and speaker_id is not None:
        conditions.append("(speaker_id = ? OR speaker_id IS NULL)")
        params.append(speaker_id)
    
    # Filtro per categorie
    if categories:
        placeholders = ','.join(['?' for _ in categories])
        conditions.append(f"category IN ({placeholders})")
        params.extend(categories)
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    
    return [{
        'timestamp': row['timestamp'],
        'category': row['category'],
        'message': row['message'],
        'speaker': row['speaker_name']
    } for row in rows]


# ===========================================================================
# CHAT MEMORY (con speaker tracking e pesi)
# ===========================================================================

def save_chat_message(
    role: str,
    content: str,
    source: str,
    speaker_id: Optional[int] = None,
    speaker_name: Optional[str] = None
):
    """Salva un messaggio nella memoria conversazionale."""
    conn = _get_conn()
    c = conn.cursor()
    timestamp = time.time()
    c.execute(
        "INSERT INTO chat_memory (timestamp, role, content, source, speaker_id, speaker_name) VALUES (?, ?, ?, ?, ?, ?)",
        (timestamp, role, content, source, speaker_id, speaker_name)
    )
    conn.commit()
    conn.close()

    # Salva anche in vector store (async-safe)
    if speaker_id:
        try:
            from vector_store import user_vector_store
            user_vector_store.add_message(
                user_id=speaker_id,
                role=role,
                content=content,
                speaker_name=speaker_name or "Unknown",
                timestamp=timestamp,
                source=source
            )
        except Exception as e:
            logging.getLogger("JARVIS").warning(f"Vector store write failed: {e}")


def get_weighted_context(
    current_speaker_id: Optional[int] = None,
    seconds: int = 3600,
    max_messages: int = 150
) -> Dict[str, List[Dict]]:
    """
    Recupera la memoria conversazionale con pesi per speaker.
    
    Args:
        current_speaker_id: ID dello speaker corrente (priorità alta)
        seconds: Finestra temporale in secondi (default 1 ora)
        max_messages: Numero massimo di messaggi da recuperare dal DB
    
    Returns:
        Dizionario con tre livelli di priorità:
        - high_priority: messaggi dello speaker corrente
        - medium_priority: messaggi di altri familiari
        - global_context: azioni di sistema (senza speaker)
    """
    conn = _get_conn()
    c = conn.cursor()
    
    cutoff = time.time() - seconds
    
    # Query tutti i messaggi recenti
    c.execute('''
        SELECT role, content, speaker_id, speaker_name, timestamp
        FROM chat_memory 
        WHERE timestamp > ?
        ORDER BY timestamp DESC
        LIMIT ?
    ''', (cutoff, max_messages))
    
    rows = c.fetchall()
    conn.close()
    
    high_priority = []
    medium_priority = []
    global_context = []
    
    for row in rows:
        msg = {
            "role": row['role'],
            "content": row['content'],
            "speaker": row['speaker_name'],
            "timestamp": row['timestamp']
        }
        
        if row['speaker_id'] is None:
            # Messaggi di sistema
            global_context.append(msg)
        elif current_speaker_id and row['speaker_id'] == current_speaker_id:
            # Messaggi dello speaker corrente
            high_priority.append(msg)
        else:
            # Messaggi di altri
            medium_priority.append(msg)
    
    # Inverti per ordine cronologico (dal più vecchio al più recente)
    return {
        "high_priority": list(reversed(high_priority)),
        "medium_priority": list(reversed(medium_priority)),
        "global_context": list(reversed(global_context))
    }


def format_weighted_context_for_llm(
    weighted_context: Dict[str, List[Dict]],
    current_speaker_name: str,
    high_limit: int = 40,
    medium_limit: int = 10,
    global_limit: int = 5
) -> str:
    """
    Formatta la memoria pesata in un prompt per l'LLM.
    
    Args:
        weighted_context: Output di get_weighted_context()
        current_speaker_name: Nome dello speaker corrente
        high_limit: Quanti messaggi high priority includere (default 40)
        medium_limit: Quanti messaggi medium priority includere (default 10)
        global_limit: Quanti messaggi global includere (default 5)
    
    Returns:
        Stringa formattata da inserire nel prompt
    """
    lines = []
    lines.append(f"[MEMORIA CONVERSAZIONALE - Speaker corrente: {current_speaker_name}]")
    lines.append("")
    
    # Alta priorità - conversazioni con lo speaker corrente
    if weighted_context["high_priority"]:
        lines.append(f"=== CONVERSAZIONI CON {current_speaker_name.upper()} (priorità alta) ===")
        for msg in weighted_context["high_priority"][-high_limit:]:
            prefix = msg['speaker'] if msg['role'] == 'user' else 'Jarvis'
            lines.append(f"- {prefix}: {msg['content']}")
        lines.append("")
    
    # Media priorità - conversazioni con altri familiari
    if weighted_context["medium_priority"]:
        lines.append("=== ALTRE CONVERSAZIONI FAMILIARI (contesto) ===")
        for msg in weighted_context["medium_priority"][-medium_limit:]:
            prefix = msg['speaker'] if msg['role'] == 'user' else 'Jarvis'
            lines.append(f"- {prefix}: {msg['content']}")
        lines.append("")
    
    # Contesto globale - eventi di sistema
    if weighted_context["global_context"]:
        lines.append("=== EVENTI DI SISTEMA ===")
        for msg in weighted_context["global_context"][-global_limit:]:
            lines.append(f"- {msg['content']}")
    
    return "\n".join(lines)


def get_recent_context(seconds: int = 3600) -> List[Dict]:
    """
    Versione semplice (backward compatible) - ritorna tutti i messaggi.
    Usare get_weighted_context per la versione con pesi.
    """
    conn = _get_conn()
    c = conn.cursor()
    cutoff = time.time() - seconds
    c.execute(
        "SELECT role, content FROM chat_memory WHERE timestamp > ? ORDER BY timestamp ASC",
        (cutoff,)
    )
    rows = c.fetchall()
    conn.close()
    return [{"role": row['role'], "content": row['content']} for row in rows]


def clear_old_chat_memory(max_age: int = None):
    """Pulisce messaggi più vecchi di 24 ore."""
    if max_age is None:
        max_age = _cfg_otp.CHAT_MEMORY_MAX_AGE
    conn = _get_conn()
    c = conn.cursor()
    cutoff = time.time() - max_age
    c.execute("DELETE FROM chat_memory WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()


# ===========================================================================
# USER PREFERENCES (per-user e globali)
# ===========================================================================

def set_user_preference(user_id: int, key: str, value: Any):
    """Imposta una preferenza specifica per utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO user_preferences (user_id, pref_key, pref_value) VALUES (?, ?, ?)",
        (user_id, key, str(value))
    )
    conn.commit()
    conn.close()


def get_user_preference(user_id: int, key: str, default: Any = None) -> Any:
    """Recupera una preferenza specifica per utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT pref_value FROM user_preferences WHERE user_id = ? AND pref_key = ?",
        (user_id, key)
    )
    row = c.fetchone()
    conn.close()
    return row['pref_value'] if row else default


def set_global_preference(key: str, value: Any):
    """Imposta una preferenza globale."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO global_preferences (pref_key, pref_value) VALUES (?, ?)",
        (key, str(value))
    )
    conn.commit()
    conn.close()


def get_global_preference(key: str, default: Any = None) -> Any:
    """Recupera una preferenza globale."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT pref_value FROM global_preferences WHERE pref_key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row['pref_value'] if row else default


def get_default_location_id() -> str:
    """Recupera il default location_id da global_preferences (runtime)."""
    import config as _cfg
    return get_global_preference("default_location_id", _cfg.DEFAULT_LOCATION_ID)


def get_llm_params(category: str) -> dict:
    """
    Recupera parametri LLM da global_preferences (runtime).

    Chiave DB: 'llm_params' → JSON dict con categorie routing/reasoning/quick_response/summary/gemini.
    Se il DB non ha la chiave, usa i default da config.LLM_PARAMS.

    Args:
        category: Una di 'routing', 'reasoning', 'quick_response', 'summary', 'gemini'

    Returns:
        Dict con temperature, max_tokens, timeout (varia per categoria)
    """
    import config as _cfg
    defaults = _cfg.LLM_PARAMS.get(category, {})

    raw = get_global_preference("llm_params", None)
    if raw:
        try:
            all_params = json.loads(raw) if isinstance(raw, str) else raw
            if category in all_params:
                # Merge: DB override + defaults for missing keys
                merged = {**defaults, **all_params[category]}
                return merged
        except (json.JSONDecodeError, TypeError):
            pass

    return defaults.copy()


# ===========================================================================
# SYSTEM STATE
# ===========================================================================

def set_system_state(key: str, value: Any):
    conn = _get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def get_system_state(key: str, default: Any = None) -> Any:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT value FROM system_state WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row['value'] if row else default


# ===========================================================================
# PENDING ACTIONS (con tracking richiesta)
# ===========================================================================

def save_action(action_id: str, payload: dict, requested_by: Optional[int] = None):
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO pending_actions (action_id, payload, requested_by, timestamp) VALUES (?, ?, ?, ?)",
        (action_id, json.dumps(payload), requested_by, time.time())
    )
    conn.commit()
    conn.close()

    # Notifica dashboard via SSE
    try:
        from event_bus import publish_sync
        publish_sync("pending_action", {
            "action_id": action_id,
            "requested_by": requested_by
        })
    except Exception:
        pass


def get_action(action_id: str) -> Optional[dict]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT payload FROM pending_actions WHERE action_id = ?", (action_id,))
    row = c.fetchone()
    conn.close()
    return json.loads(row['payload']) if row else None


def delete_action(action_id: str):
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM pending_actions WHERE action_id = ?", (action_id,))
    conn.commit()
    conn.close()


def cleanup_old_actions(timeout: int = 3600):
    conn = _get_conn()
    c = conn.cursor()
    cutoff = time.time() - timeout
    c.execute("DELETE FROM pending_actions WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()


# ===========================================================================
# TELEGRAM STREAMING
# ===========================================================================

def save_telegram_stream(source_id: str, message_id: int):
    conn = _get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO telegram_streams VALUES (?, ?, ?)", 
              (source_id, message_id, time.time()))
    conn.commit()
    conn.close()


def get_telegram_stream(source_id: str) -> Optional[int]:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT message_id FROM telegram_streams WHERE source_id = ?", (source_id,))
    row = c.fetchone()
    conn.close()
    return row['message_id'] if row else None


def clear_telegram_stream(source_id: str):
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM telegram_streams WHERE source_id = ?", (source_id,))
    conn.commit()
    conn.close()


# ===========================================================================
# DEVICE FAILURES
# ===========================================================================

def increment_device_failure(entity_id: str, error_msg: str):
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO device_failures (entity_id, count, last_error, timestamp)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET
        count = count + 1, last_error = ?, timestamp = ?
    """, (entity_id, error_msg, time.time(), error_msg, time.time()))
    conn.commit()
    conn.close()


def reset_device_failure(entity_id: str):
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM device_failures WHERE entity_id = ?", (entity_id,))
    conn.commit()
    conn.close()


# ===========================================================================
# SMART CACHE
# ===========================================================================

class SmartCache:
    """Sistema di caching auto-apprendente."""
    
    def __init__(self):
        self.static_patterns = {
            r"che ore sono": lambda: f"Sono le {time.strftime('%H:%M')}",
            r"che giorno.*(è|oggi)": lambda: f"Oggi è {time.strftime('%A %d %B')}",
            r"^(ciao|hey|buongiorno|buonasera|salve)[\s\!\.]*$": lambda: "Ciao! Come posso aiutarti?",
            r"^grazie[\s\!\.]*$": lambda: "Di nulla!",
            r"^ok[\s\!\.]*$": lambda: "Perfetto!",
        }
    
    def check(self, query: str) -> Optional[str]:
        query_lower = query.lower().strip()
        
        for pattern, response_fn in self.static_patterns.items():
            if re.search(pattern, query_lower):
                return response_fn()
        
        query_hash = self._hash(query_lower)
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT response FROM query_cache WHERE query_hash = ?", (query_hash,))
        row = c.fetchone()
        
        if row:
            c.execute(
                "UPDATE query_cache SET hit_count = hit_count + 1, last_used = ? WHERE query_hash = ?",
                (time.time(), query_hash)
            )
            conn.commit()
            conn.close()
            return row['response']
        
        conn.close()
        return None
    
    def learn(self, query: str, response: str, intent: str):
        if intent in ["DEEP_REASONING", "RESEARCH", "AUDIT_REPORT", "SECURITY_ALERT"]:
            return
        
        query_normalized = self._normalize(query)
        conn = _get_conn()
        c = conn.cursor()
        
        c.execute("""
            INSERT INTO query_frequency (query_normalized, count, last_response, response_consistent)
            VALUES (?, 1, ?, TRUE)
            ON CONFLICT(query_normalized) DO UPDATE SET
            count = count + 1,
            response_consistent = CASE WHEN last_response = ? THEN response_consistent ELSE FALSE END,
            last_response = ?
        """, (query_normalized, response, response, response))
        
        c.execute("SELECT count, response_consistent FROM query_frequency WHERE query_normalized = ?", 
                  (query_normalized,))
        row = c.fetchone()
        
        if row and row['count'] >= _cfg_otp.AUTO_LEARN_FREQUENCY_THRESHOLD and row['response_consistent']:
            query_hash = self._hash(query.lower().strip())
            c.execute("""
                INSERT OR REPLACE INTO query_cache 
                (query_hash, query_text, response, hit_count, last_used, auto_learned)
                VALUES (?, ?, ?, 1, ?, TRUE)
            """, (query_hash, query, response, time.time()))
        
        conn.commit()
        conn.close()
    
    def _hash(self, text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()
    
    def _normalize(self, text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r'\b(il|lo|la|i|gli|le|un|una|uno|e|o|ma|che|di|da|in|con|su|per|tra|fra)\b', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text


# Istanza globale
smart_cache = SmartCache()


# ===========================================================================
# LOCATIONS (Multi-Home Assistant)
# ===========================================================================

@dataclass
class Location:
    id: str
    name: str
    city: Optional[str]
    hass_url: str
    hass_token: str
    entity_map_path: Optional[str]
    has_security: bool
    enabled: bool
    keywords: List[str]  # Lista di parole chiave per identificare la location
    created_at: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "city": self.city,
            "hass_url": self.hass_url,
            "hass_token": self.hass_token,
            "entity_map_path": self.entity_map_path,
            "has_security": self.has_security,
            "enabled": self.enabled,
            "keywords": self.keywords,
            "created_at": self.created_at
        }


def _parse_keywords(keywords_str: Optional[str]) -> List[str]:
    """Parsa stringa keywords JSON in lista."""
    if not keywords_str:
        return []
    try:
        return json.loads(keywords_str)
    except (json.JSONDecodeError, TypeError):
        # Fallback: split per virgola se non è JSON
        return [k.strip().lower() for k in keywords_str.split(",") if k.strip()]


def get_all_locations(enabled_only: bool = True) -> List[Location]:
    """Recupera tutte le location configurate."""
    conn = _get_conn()
    c = conn.cursor()

    if enabled_only:
        c.execute("SELECT * FROM locations WHERE enabled = TRUE ORDER BY name")
    else:
        c.execute("SELECT * FROM locations ORDER BY name")

    rows = c.fetchall()
    conn.close()

    return [Location(
        id=row['id'],
        name=row['name'],
        city=row['city'],
        hass_url=row['hass_url'],
        hass_token=row['hass_token'],
        entity_map_path=row['entity_map_path'],
        has_security=bool(row['has_security']),
        enabled=bool(row['enabled']),
        keywords=_parse_keywords(row['keywords']),
        created_at=row['created_at']
    ) for row in rows]


def get_location(location_id: str) -> Optional[Location]:
    """Recupera una location per ID."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM locations WHERE id = ?", (location_id,))
    row = c.fetchone()
    conn.close()

    if row:
        return Location(
            id=row['id'],
            name=row['name'],
            city=row['city'],
            hass_url=row['hass_url'],
            hass_token=row['hass_token'],
            entity_map_path=row['entity_map_path'],
            has_security=bool(row['has_security']),
            enabled=bool(row['enabled']),
            keywords=_parse_keywords(row['keywords']),
            created_at=row['created_at']
        )
    return None


def create_location(
    id: str,
    name: str,
    city: str,
    hass_url: str,
    hass_token: str,
    entity_map_path: str = None,
    has_security: bool = False,
    keywords: List[str] = None
) -> bool:
    """Crea una nuova location."""
    conn = _get_conn()
    c = conn.cursor()
    keywords_json = json.dumps(keywords) if keywords else None
    try:
        c.execute("""
            INSERT INTO locations (id, name, city, hass_url, hass_token, entity_map_path, has_security, keywords, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, TRUE)
        """, (id, name, city, hass_url, hass_token, entity_map_path, has_security, keywords_json))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def update_location(location_id: str, **kwargs) -> bool:
    """Aggiorna campi di una location."""
    if not kwargs:
        return False

    allowed_fields = {'name', 'city', 'hass_url', 'hass_token', 'entity_map_path', 'has_security', 'enabled', 'keywords'}

    # Converti keywords in JSON se è una lista
    if 'keywords' in kwargs and isinstance(kwargs['keywords'], list):
        kwargs['keywords'] = json.dumps(kwargs['keywords'])

    update_fields = {k: v for k, v in kwargs.items() if k in allowed_fields}

    if not update_fields:
        return False

    conn = _get_conn()
    c = conn.cursor()

    set_clause = ", ".join([f"{k} = ?" for k in update_fields.keys()])
    values = list(update_fields.values()) + [location_id]

    c.execute(f"UPDATE locations SET {set_clause} WHERE id = ?", values)
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def delete_location(location_id: str) -> bool:
    """Disabilita una location (soft delete)."""
    return update_location(location_id, enabled=False)


# ===========================================================================
# USER LOCATIONS (Location corrente per utente)
# ===========================================================================

@dataclass
class UserLocation:
    user_id: int
    location_id: str
    source: str
    updated_at: float

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "location_id": self.location_id,
            "source": self.source,
            "updated_at": self.updated_at
        }


def get_user_location(user_id: int) -> Optional[UserLocation]:
    """Recupera la location corrente di un utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM user_locations WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()

    if row:
        return UserLocation(
            user_id=row['user_id'],
            location_id=row['location_id'],
            source=row['source'],
            updated_at=row['updated_at']
        )
    return None


def set_user_location(user_id: int, location_id: str, source: str) -> bool:
    """Imposta la location corrente di un utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO user_locations (user_id, location_id, source, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
        location_id = ?, source = ?, updated_at = ?
    """, (user_id, location_id, source, time.time(), location_id, source, time.time()))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def clear_user_location(user_id: int) -> bool:
    """Rimuove la location sticky di un utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM user_locations WHERE user_id = ?", (user_id,))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


# ===========================================================================
# ENTITY MAPS (Struttura stanze/dispositivi per location)
# ===========================================================================

def get_entity_map(location_id: str, include_entity_ids: bool = False,
                    exclude_unassigned: bool = False) -> dict:
    """
    Recupera l'entity map per una location in formato gerarchico.

    Se le entity hanno device_name popolato (da HA sync), usa la struttura:
        Zone → Area → Room → Device → EntityType → [Entities]

    Altrimenti fallback alla struttura legacy:
        Zone → Area → Room → EntityType → [Entities]

    Args:
        location_id: ID della location
        include_entity_ids: Se True, include entity_id nell'output (per export completo)
                           Se False, ritorna solo friendly names (per LLM/prompt)
        exclude_unassigned: Se True, esclude entity senza area assegnata su HA
                           (room = "Sconosciuto" / zone|area = "Non classificato")
    """
    conn = _get_conn()
    c = conn.cursor()

    if exclude_unassigned:
        c.execute("""
            SELECT zone, area, room, device_name, entity_type, entity_name, entity_id
            FROM entity_maps
            WHERE location_id = ?
              AND LOWER(room) != 'sconosciuto'
              AND LOWER(zone) != 'non classificato'
            ORDER BY zone, area, room, device_name, entity_type
        """, (location_id,))
    else:
        c.execute("""
            SELECT zone, area, room, device_name, entity_type, entity_name, entity_id
            FROM entity_maps
            WHERE location_id = ?
            ORDER BY zone, area, room, device_name, entity_type
        """, (location_id,))
    rows = c.fetchall()
    conn.close()

    # Verifica se abbiamo device_name popolati
    has_devices = any(row['device_name'] for row in rows)

    # Costruisci struttura gerarchica
    entity_map = {}
    for row in rows:
        zone = row['zone']
        area = row['area']
        room = row['room']
        device_name = row['device_name']
        entity_type = row['entity_type']
        entity_name = row['entity_name']
        entity_id = row['entity_id']

        if zone not in entity_map:
            entity_map[zone] = {}
        if area not in entity_map[zone]:
            entity_map[zone][area] = {}
        if room not in entity_map[zone][area]:
            entity_map[zone][area][room] = {}

        if has_devices:
            # Struttura con device: Room → Device → EntityType → [Entities]
            # Entity senza device vanno in un gruppo "Altro"
            dev_key = device_name or f"Altro ({room})"
            if dev_key not in entity_map[zone][area][room]:
                entity_map[zone][area][room][dev_key] = {}
            if entity_type not in entity_map[zone][area][room][dev_key]:
                entity_map[zone][area][room][dev_key][entity_type] = []

            if include_entity_ids and entity_id:
                entity_map[zone][area][room][dev_key][entity_type].append({
                    "name": entity_name,
                    "entity_id": entity_id
                })
            else:
                entity_map[zone][area][room][dev_key][entity_type].append(entity_name)
        else:
            # Struttura legacy senza device: Room → EntityType → [Entities]
            if entity_type not in entity_map[zone][area][room]:
                entity_map[zone][area][room][entity_type] = []

            if include_entity_ids and entity_id:
                entity_map[zone][area][room][entity_type].append({
                    "name": entity_name,
                    "entity_id": entity_id
                })
            else:
                entity_map[zone][area][room][entity_type].append(entity_name)

    return entity_map


def get_entity_map_for_llm(location_id: str) -> dict:
    """
    Versione semplificata dell'entity map per il LLM (solo friendly names).
    Esclude entity senza area assegnata su HA (room = "Sconosciuto").
    """
    return get_entity_map(location_id, include_entity_ids=False, exclude_unassigned=True)


def import_entity_map_json(location_id: str, entity_map_json: dict) -> int:
    """
    Importa un entity map da JSON nel database.
    Cancella tutti i record esistenti per la location e li ricrea.

    Supporta tre formati per le entity:
    1. Solo friendly name (string): "Luce Soggiorno"
    2. Dict con entity_id: {"name": "Luce Soggiorno", "entity_id": "light.shelly_abc123"}
    3. Dict con id shorthand: {"name": "Echo Camera", "id": "media_player.alexa_media_radiosveglia"}

    Struttura gerarchica:
    - Formato completo: Zone → Area → Room → EntityType → [Entities]
    - Formato semplificato: Zone → Room → EntityType → [Entities]

    Returns:
        Numero di entity importate
    """
    conn = _get_conn()
    c = conn.cursor()

    # Elimina entity esistenti per questa location
    c.execute("DELETE FROM entity_maps WHERE location_id = ?", (location_id,))

    count = 0

    def _parse_entity(entity_data) -> tuple:
        """
        Parsa un'entity e ritorna (friendly_name, entity_id).
        Supporta sia string che dict.
        """
        if isinstance(entity_data, str):
            # Formato semplice: solo nome
            return entity_data, None
        elif isinstance(entity_data, dict):
            # Formato con entity_id
            name = entity_data.get("name", entity_data.get("friendly_name", ""))
            entity_id = entity_data.get("entity_id", entity_data.get("id"))
            return name, entity_id
        return None, None

    def _parse_room(zone: str, area: str, room: str, room_data: dict):
        """Parsa i dati di una stanza."""
        nonlocal count
        for entity_type, entities in room_data.items():
            if isinstance(entities, list):
                for entity_data in entities:
                    entity_name, entity_id = _parse_entity(entity_data)
                    if entity_name:
                        c.execute("""
                            INSERT OR IGNORE INTO entity_maps
                            (location_id, zone, area, room, entity_type, entity_name, entity_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (location_id, zone, area, room, entity_type, entity_name, entity_id))
                        count += 1

    def _is_room_data(data: dict) -> bool:
        """Verifica se un dict rappresenta dati di una stanza (ha entity types)."""
        entity_types = {'light', 'switch', 'cover', 'sensor', 'climate',
                        'media_player', 'camera', 'lock', 'event', 'binary_sensor',
                        'fan', 'vacuum', 'alarm_control_panel', 'scene', 'script',
                        'input_boolean', 'input_number', 'input_select', 'automation'}
        return any(key in entity_types for key in data.keys())

    # Parse struttura gerarchica
    for zone_name, zone_data in entity_map_json.items():
        if not isinstance(zone_data, dict):
            continue

        # Caso speciale: "Impianti" - nessuna area/room, solo entity types
        if _is_room_data(zone_data):
            _parse_room(zone_name, zone_name, zone_name, zone_data)
            continue

        for area_or_room_name, area_or_room_data in zone_data.items():
            if not isinstance(area_or_room_data, dict):
                continue

            # Se è direttamente room data (formato semplificato)
            if _is_room_data(area_or_room_data):
                _parse_room(zone_name, zone_name, area_or_room_name, area_or_room_data)
            else:
                # È un'area con stanze dentro
                area_name = area_or_room_name
                for room_name, room_data in area_or_room_data.items():
                    if isinstance(room_data, dict):
                        if _is_room_data(room_data):
                            _parse_room(zone_name, area_name, room_name, room_data)
                        else:
                            # Ancora un livello (sub-area)
                            for sub_room_name, sub_room_data in room_data.items():
                                if isinstance(sub_room_data, dict) and _is_room_data(sub_room_data):
                                    _parse_room(zone_name, area_name, sub_room_name, sub_room_data)

    conn.commit()
    conn.close()
    return count


def import_hierarchy_json(location_id: str, hierarchy_json: dict) -> dict:
    """
    Importa una gerarchia personalizzata (Piani -> Zone -> Aree/Stanze HA).
    NON importa entity — crea solo la struttura. Le entity vengono poi
    popolate dal sync con Home Assistant.

    Formato atteso:
    {
      "floors": [
        {
          "floor_name": "Piano 1",
          "zones": [
            {
              "zone_name": "Zona Giorno",      # opzionale, null = usa floor_name
              "ha_areas": ["cucina", "soggiorno"]
            }
          ]
        }
      ]
    }

    Mapping al DB:
    - floor_name → colonna 'zone'
    - zone_name → colonna 'area' (se null, usa floor_name)
    - ha_areas → colonna 'room'

    Returns:
        Dict con statistiche: floors, zones, rooms creati
    """
    conn = _get_conn()
    c = conn.cursor()

    floors_count = 0
    zones_count = 0
    rooms_count = 0
    ha_area_to_hierarchy = {}  # mapping ha_area -> (zone, area)

    floors = hierarchy_json.get("floors", [])

    for floor in floors:
        floor_name = floor.get("floor_name", "Sconosciuto")
        floors_count += 1

        for zone in floor.get("zones", []):
            zone_name = zone.get("zone_name") or floor_name
            zones_count += 1

            for ha_area in zone.get("ha_areas", []):
                rooms_count += 1
                ha_area_to_hierarchy[ha_area] = (floor_name, zone_name)

    # Salva il mapping come metadata nella location
    # (usato poi dal sync HA per piazzare le entity nella gerarchia corretta)
    import json
    loc = get_location(location_id)
    if loc:
        c.execute("""
            UPDATE locations SET entity_map_path = ? WHERE id = ?
        """, (json.dumps({
            "type": "hierarchy",
            "ha_area_mapping": ha_area_to_hierarchy,
            "source": hierarchy_json
        }), location_id))

    conn.commit()
    conn.close()

    logger.info(f"Hierarchy imported for {location_id}: "
                f"{floors_count} floors, {zones_count} zones, {rooms_count} rooms")

    return {
        "floors": floors_count,
        "zones": zones_count,
        "rooms": rooms_count,
        "ha_area_mapping": ha_area_to_hierarchy
    }


def get_hierarchy_mapping(location_id: str) -> dict:
    """
    Ritorna il mapping ha_area → (zone, area) salvato dalla gerarchia importata.
    Usato da ha_sync per piazzare le entity nella posizione corretta.

    Returns:
        Dict ha_area_name → {"zone": floor_name, "area": zone_name}
        oppure dict vuoto se nessuna gerarchia importata.
    """
    import json
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT entity_map_path FROM locations WHERE id = ?", (location_id,))
    row = c.fetchone()
    conn.close()

    if not row or not row['entity_map_path']:
        return {}

    try:
        data = json.loads(row['entity_map_path'])
        if data.get("type") == "hierarchy":
            mapping = data.get("ha_area_mapping", {})
            # Converti da {area: [floor, zone]} a {area: {"zone": floor, "area": zone}}
            result = {}
            for ha_area, (floor, zone) in mapping.items():
                result[ha_area] = {"zone": floor, "area": zone}
            return result
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    return {}


def export_entity_map_json(location_id: str) -> dict:
    """
    Esporta l'entity map di una location in formato JSON.
    Alias di get_entity_map per chiarezza semantica nell'API.
    """
    return get_entity_map(location_id)


def get_entity_map_stats(location_id: str) -> dict:
    """Ritorna statistiche sull'entity map di una location."""
    conn = _get_conn()
    c = conn.cursor()

    c.execute("""
        SELECT COUNT(*) as total,
               COUNT(DISTINCT room) as rooms,
               COUNT(DISTINCT entity_type) as types
        FROM entity_maps WHERE location_id = ?
    """, (location_id,))
    row = c.fetchone()

    c.execute("""
        SELECT entity_type, COUNT(*) as count
        FROM entity_maps WHERE location_id = ?
        GROUP BY entity_type
    """, (location_id,))
    type_counts = {r['entity_type']: r['count'] for r in c.fetchall()}

    conn.close()

    return {
        "total_entities": row['total'] if row else 0,
        "rooms": row['rooms'] if row else 0,
        "entity_types": row['types'] if row else 0,
        "by_type": type_counts
    }


def clear_entity_map(location_id: str) -> int:
    """Cancella tutte le entity di una location. Ritorna il numero di record eliminati."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM entity_maps WHERE location_id = ?", (location_id,))
    conn.commit()
    deleted = c.rowcount
    conn.close()
    return deleted


def get_rooms_for_location(location_id: str) -> List[str]:
    """Ritorna la lista delle stanze per una location."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT room FROM entity_maps
        WHERE location_id = ?
        ORDER BY room
    """, (location_id,))
    rooms = [row['room'] for row in c.fetchall()]
    conn.close()
    return rooms


def get_entities_by_room(location_id: str, room: str) -> dict:
    """Ritorna le entity di una stanza specifica, raggruppate per tipo."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT entity_type, entity_name FROM entity_maps
        WHERE location_id = ? AND room = ?
        ORDER BY entity_type
    """, (location_id, room))
    rows = c.fetchall()
    conn.close()

    result = {}
    for row in rows:
        entity_type = row['entity_type']
        if entity_type not in result:
            result[entity_type] = []
        result[entity_type].append(row['entity_name'])

    return result


def get_entities_by_type(location_id: str, entity_type: str) -> List[dict]:
    """Ritorna tutte le entity di un tipo specifico con la loro posizione."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT zone, area, room, entity_name, entity_id FROM entity_maps
        WHERE location_id = ? AND entity_type = ?
        ORDER BY zone, area, room
    """, (location_id, entity_type))
    rows = c.fetchall()
    conn.close()

    return [{
        "zone": row['zone'],
        "area": row['area'],
        "room": row['room'],
        "name": row['entity_name'],
        "entity_id": row['entity_id']
    } for row in rows]


# ===========================================================================
# ENTITY DISCOVERY FOR VOICE (Room/Zone/Floor → entity_id list)
# ===========================================================================

def discover_entities_for_voice(
    location_id: str,
    target_name: str,
    domain: str = None
) -> List[dict]:
    """
    Cerca entity matchando target_name come stanza, zona, piano o 'tutto'.

    Usato dal flusso DOMOTICA_CERTA per risolvere comandi come:
    - "spegni le luci in cucina" → tutte le light nella room Cucina
    - "chiudi le tapparelle zona notte" → tutte le cover nella zona Notte
    - "spegni tutto" → tutte le entity del dominio nella location

    Args:
        location_id: ID della location
        target_name: Nome target (stanza, zona, piano, o "tutto"/"tutte")
        domain: Dominio opzionale per filtrare (es. "light", "cover")

    Returns:
        Lista di dict con entity_id, entity_name, room, match_type
        Vuota se nessun match trovato.
    """
    conn = _get_conn()
    c = conn.cursor()
    target_lower = target_name.lower().strip()

    # Caso "tutto" / "tutte" / "tutta la casa" → tutte le entity del dominio
    if target_lower in ("tutto", "tutti", "tutte", "tutta la casa", "ovunque", "dappertutto"):
        query = """
            SELECT entity_id, entity_name, entity_type, room, area, zone
            FROM entity_maps
            WHERE location_id = ? AND entity_id IS NOT NULL
        """
        params: list = [location_id]
        if domain:
            query += " AND entity_type = ?"
            params.append(domain)
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        return [{"entity_id": r["entity_id"], "entity_name": r["entity_name"],
                 "room": r["room"], "match_type": "all"} for r in rows]

    results = []

    # 1. Match per stanza (room) — priorità più alta
    query = """
        SELECT entity_id, entity_name, entity_type, room, area, zone
        FROM entity_maps
        WHERE location_id = ? AND entity_id IS NOT NULL
          AND LOWER(room) LIKE ?
    """
    params = [location_id, f"%{target_lower}%"]
    if domain:
        query += " AND entity_type = ?"
        params.append(domain)
    c.execute(query, params)
    rows = c.fetchall()
    if rows:
        results = [{"entity_id": r["entity_id"], "entity_name": r["entity_name"],
                     "room": r["room"], "match_type": "room"} for r in rows]
        conn.close()
        return results

    # 2. Match per zona (area) — es. "Zona Giorno", "Zona Notte"
    query = """
        SELECT entity_id, entity_name, entity_type, room, area, zone
        FROM entity_maps
        WHERE location_id = ? AND entity_id IS NOT NULL
          AND LOWER(area) LIKE ?
    """
    params = [location_id, f"%{target_lower}%"]
    if domain:
        query += " AND entity_type = ?"
        params.append(domain)
    c.execute(query, params)
    rows = c.fetchall()
    if rows:
        results = [{"entity_id": r["entity_id"], "entity_name": r["entity_name"],
                     "room": r["room"], "match_type": "zone"} for r in rows]
        conn.close()
        return results

    # 3. Match per piano (zone) — es. "Piano 1", "Piano Terra"
    query = """
        SELECT entity_id, entity_name, entity_type, room, area, zone
        FROM entity_maps
        WHERE location_id = ? AND entity_id IS NOT NULL
          AND LOWER(zone) LIKE ?
    """
    params = [location_id, f"%{target_lower}%"]
    if domain:
        query += " AND entity_type = ?"
        params.append(domain)
    c.execute(query, params)
    rows = c.fetchall()
    if rows:
        results = [{"entity_id": r["entity_id"], "entity_name": r["entity_name"],
                     "room": r["room"], "match_type": "floor"} for r in rows]
        conn.close()
        return results

    conn.close()
    return []


# ===========================================================================
# ENTITY RESOLUTION (Friendly Name → Entity ID)
# ===========================================================================

def resolve_entity_id(
    location_id: str,
    friendly_name: str,
    entity_type: str = None,
    room: str = None
) -> Optional[str]:
    """
    Risolve un friendly name nel corrispondente entity_id di Home Assistant.

    Args:
        location_id: ID della location
        friendly_name: Nome friendly dell'entità (es: "Echo Camera", "Luce Soggiorno")
        entity_type: Tipo opzionale per restringere la ricerca (es: "media_player", "light")
        room: Stanza opzionale per disambiguare entità con stesso nome

    Returns:
        entity_id di HA (es: "media_player.alexa_media_radiosveglia") o None se non trovato

    Examples:
        resolve_entity_id("wagmi", "Echo Camera") → "media_player.alexa_media_radiosveglia"
        resolve_entity_id("wagmi", "Luce Principale", room="Soggiorno") → "light.shelly_abc123"
    """
    conn = _get_conn()
    c = conn.cursor()

    friendly_lower = friendly_name.lower().strip()

    # Costruisci query in base ai parametri
    if entity_type and room:
        c.execute("""
            SELECT entity_id, entity_name FROM entity_maps
            WHERE location_id = ? AND entity_type = ? AND LOWER(room) = LOWER(?)
            AND entity_id IS NOT NULL
        """, (location_id, entity_type, room))
    elif entity_type:
        c.execute("""
            SELECT entity_id, entity_name FROM entity_maps
            WHERE location_id = ? AND entity_type = ?
            AND entity_id IS NOT NULL
        """, (location_id, entity_type))
    elif room:
        c.execute("""
            SELECT entity_id, entity_name FROM entity_maps
            WHERE location_id = ? AND LOWER(room) = LOWER(?)
            AND entity_id IS NOT NULL
        """, (location_id, room))
    else:
        c.execute("""
            SELECT entity_id, entity_name FROM entity_maps
            WHERE location_id = ? AND entity_id IS NOT NULL
        """, (location_id,))

    rows = c.fetchall()
    conn.close()

    # Cerca match esatto
    for row in rows:
        if row['entity_name'].lower().strip() == friendly_lower:
            return row['entity_id']

    # Cerca match parziale (friendly_name contenuto in entity_name o viceversa)
    for row in rows:
        entity_name_lower = row['entity_name'].lower().strip()
        if friendly_lower in entity_name_lower or entity_name_lower in friendly_lower:
            return row['entity_id']

    return None


def resolve_entity_id_fuzzy(
    location_id: str,
    friendly_name: str,
    entity_type: str = None
) -> List[dict]:
    """
    Cerca entity che matchano approssimativamente il friendly name.
    Utile per suggerimenti o quando non c'è match esatto.

    Returns:
        Lista di match con score: [{"entity_id": "...", "entity_name": "...", "room": "...", "score": 0.8}]
    """
    conn = _get_conn()
    c = conn.cursor()

    if entity_type:
        c.execute("""
            SELECT entity_id, entity_name, room, entity_type FROM entity_maps
            WHERE location_id = ? AND entity_type = ?
            AND entity_id IS NOT NULL
        """, (location_id, entity_type))
    else:
        c.execute("""
            SELECT entity_id, entity_name, room, entity_type FROM entity_maps
            WHERE location_id = ? AND entity_id IS NOT NULL
        """, (location_id,))

    rows = c.fetchall()
    conn.close()

    friendly_lower = friendly_name.lower().strip()
    friendly_words = set(friendly_lower.split())
    results = []

    for row in rows:
        entity_name_lower = row['entity_name'].lower().strip()
        entity_words = set(entity_name_lower.split())

        # Calcola score basato su parole in comune
        common_words = friendly_words & entity_words
        if common_words:
            score = len(common_words) / max(len(friendly_words), len(entity_words))
            results.append({
                "entity_id": row['entity_id'],
                "entity_name": row['entity_name'],
                "entity_type": row['entity_type'],
                "room": row['room'],
                "score": score
            })

    # Ordina per score decrescente
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:5]  # Top 5 match


def get_entity_by_id(location_id: str, entity_id: str) -> Optional[dict]:
    """
    Cerca un'entità per entity_id.
    Utile per il reverse lookup (entity_id → friendly name e posizione).
    """
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT zone, area, room, entity_type, entity_name, entity_id
        FROM entity_maps
        WHERE location_id = ? AND entity_id = ?
    """, (location_id, entity_id))
    row = c.fetchone()
    conn.close()

    if row:
        return {
            "zone": row['zone'],
            "area": row['area'],
            "room": row['room'],
            "entity_type": row['entity_type'],
            "entity_name": row['entity_name'],
            "entity_id": row['entity_id']
        }
    return None


def update_entity_id(
    location_id: str,
    entity_name: str,
    entity_id: str,
    entity_type: str = None,
    room: str = None
) -> bool:
    """
    Aggiorna l'entity_id per un'entità esistente.
    Utile per correggere mapping o aggiungere entity_id mancanti.

    Returns:
        True se aggiornato, False se entità non trovata
    """
    conn = _get_conn()
    c = conn.cursor()

    if entity_type and room:
        c.execute("""
            UPDATE entity_maps SET entity_id = ?
            WHERE location_id = ? AND LOWER(entity_name) = LOWER(?)
            AND entity_type = ? AND LOWER(room) = LOWER(?)
        """, (entity_id, location_id, entity_name, entity_type, room))
    elif entity_type:
        c.execute("""
            UPDATE entity_maps SET entity_id = ?
            WHERE location_id = ? AND LOWER(entity_name) = LOWER(?)
            AND entity_type = ?
        """, (entity_id, location_id, entity_name, entity_type))
    elif room:
        c.execute("""
            UPDATE entity_maps SET entity_id = ?
            WHERE location_id = ? AND LOWER(entity_name) = LOWER(?)
            AND LOWER(room) = LOWER(?)
        """, (entity_id, location_id, entity_name, room))
    else:
        c.execute("""
            UPDATE entity_maps SET entity_id = ?
            WHERE location_id = ? AND LOWER(entity_name) = LOWER(?)
        """, (entity_id, location_id, entity_name))

    updated = c.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_entities_without_id(location_id: str) -> List[dict]:
    """
    Ritorna tutte le entità che non hanno entity_id configurato.
    Utile per identificare mapping incompleti.
    """
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT zone, area, room, entity_type, entity_name
        FROM entity_maps
        WHERE location_id = ? AND (entity_id IS NULL OR entity_id = '')
        ORDER BY room, entity_type
    """, (location_id,))
    rows = c.fetchall()
    conn.close()

    return [{
        "zone": row['zone'],
        "area": row['area'],
        "room": row['room'],
        "entity_type": row['entity_type'],
        "entity_name": row['entity_name']
    } for row in rows]


def find_location_by_keyword(keyword: str) -> Optional[str]:
    """
    Trova una location che matcha la keyword.
    Ritorna l'ID della location o None.
    """
    keyword = keyword.lower().strip()
    locations = get_all_locations(enabled_only=True)

    for loc in locations:
        # Check ID diretto
        if loc.id.lower() == keyword:
            return loc.id
        # Check keywords
        if keyword in [k.lower() for k in loc.keywords]:
            return loc.id
        # Check nome e città
        if keyword in loc.name.lower() or (loc.city and keyword in loc.city.lower()):
            return loc.id

    return None


def get_location_keywords_map() -> Dict[str, List[str]]:
    """
    Ritorna un dizionario location_id -> keywords per tutte le location abilitate.
    Usato dal router per identificare location nei comandi.
    """
    locations = get_all_locations(enabled_only=True)
    return {loc.id: loc.keywords for loc in locations}


def get_cameras_by_category(location_id: str, category: str) -> List[str]:
    """
    Ritorna le camere di una categoria specifica (internal/perimetral).
    Le camere devono essere taggate nella zona corrispondente nell'entity_map.

    Args:
        location_id: ID della location
        category: "internal" o "perimetral"

    Returns:
        Lista di entity_id delle camere (es: ["camera.cucina", "camera.salotto"])
    """
    conn = _get_conn()
    c = conn.cursor()

    # Cerca camere nella zona corrispondente
    zone_names = {
        "internal": ["interno", "internal", "interne"],
        "perimetral": ["esterno", "perimetral", "perimetrale", "external", "esterne"]
    }

    zones = zone_names.get(category.lower(), [category.lower()])

    cameras = []
    for zone in zones:
        c.execute("""
            SELECT entity_name FROM entity_maps
            WHERE location_id = ? AND entity_type = 'camera' AND LOWER(zone) LIKE ?
            ORDER BY entity_name
        """, (location_id, f"%{zone}%"))
        for row in c.fetchall():
            entity_name = row['entity_name'].lower().replace(" ", "_")
            cameras.append(f"camera.{entity_name}")

    conn.close()
    return cameras


def get_all_cameras(location_id: str) -> Dict[str, List[str]]:
    """
    Ritorna tutte le camere di una location raggruppate per categoria.

    Returns:
        {"internal": [...], "perimetral": [...], "other": [...]}
    """
    return {
        "internal": get_cameras_by_category(location_id, "internal"),
        "perimetral": get_cameras_by_category(location_id, "perimetral"),
    }


# ===========================================================================
# VOICE DEVICES (configurazione AtomS3R)
# ===========================================================================

@dataclass
class VoiceDevice:
    device_id: str  # MAC address senza separatori (AABBCCDDEEFF)
    friendly_name: Optional[str]
    location_id: Optional[str]
    output_speaker: str
    fallback_speaker: Optional[str]
    fallback_telegram: bool
    fallback_local_speaker: bool
    enabled: bool
    created_at: float
    last_seen_at: Optional[float]
    firmware_version: Optional[str]
    ip_address: Optional[str]
    notes: Optional[str]

    @property
    def is_configured(self) -> bool:
        """Un device è configurato se ha un friendly_name."""
        return self.friendly_name is not None

    @property
    def is_online(self) -> bool:
        """Un device è online se last_seen < 10 minuti fa."""
        if self.last_seen_at is None:
            return False
        return (time.time() - self.last_seen_at) < 600  # 10 minuti

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "friendly_name": self.friendly_name,
            "location_id": self.location_id,
            "output_speaker": self.output_speaker,
            "fallback_speaker": self.fallback_speaker,
            "fallback_telegram": self.fallback_telegram,
            "fallback_local_speaker": self.fallback_local_speaker,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "firmware_version": self.firmware_version,
            "ip_address": self.ip_address,
            "notes": self.notes,
            "is_configured": self.is_configured,
            "is_online": self.is_online
        }


def _row_to_voice_device(row) -> VoiceDevice:
    """Helper per costruire VoiceDevice da sqlite row."""
    return VoiceDevice(
        device_id=row['device_id'],
        friendly_name=row['friendly_name'],
        location_id=row['location_id'],
        output_speaker=row['output_speaker'],
        fallback_speaker=row['fallback_speaker'],
        fallback_telegram=bool(row['fallback_telegram']),
        fallback_local_speaker=bool(row['fallback_local_speaker']),
        enabled=bool(row['enabled']),
        created_at=row['created_at'],
        last_seen_at=row['last_seen_at'],
        firmware_version=row['firmware_version'],
        ip_address=row['ip_address'],
        notes=row['notes']
    )


def get_voice_device(device_id: str) -> Optional[VoiceDevice]:
    """Recupera un voice device per device_id (MAC address)."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM voice_devices WHERE device_id = ?", (device_id.upper(),))
    row = c.fetchone()
    conn.close()
    if row:
        return _row_to_voice_device(row)
    return None


def get_all_voice_devices() -> List[VoiceDevice]:
    """Recupera tutti i voice devices."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM voice_devices ORDER BY friendly_name, device_id")
    rows = c.fetchall()
    conn.close()
    return [_row_to_voice_device(row) for row in rows]


def get_voice_devices_by_location(location_id: str) -> List[VoiceDevice]:
    """Recupera i voice devices di una specifica location."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM voice_devices WHERE location_id = ? ORDER BY friendly_name",
        (location_id,)
    )
    rows = c.fetchall()
    conn.close()
    return [_row_to_voice_device(row) for row in rows]


def get_unconfigured_voice_devices() -> List[VoiceDevice]:
    """Recupera i device visti ma non ancora configurati (friendly_name IS NULL)."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM voice_devices WHERE friendly_name IS NULL ORDER BY last_seen_at DESC"
    )
    rows = c.fetchall()
    conn.close()
    return [_row_to_voice_device(row) for row in rows]


def get_configured_voice_devices() -> List[VoiceDevice]:
    """Recupera i device configurati (friendly_name IS NOT NULL)."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM voice_devices WHERE friendly_name IS NOT NULL ORDER BY friendly_name"
    )
    rows = c.fetchall()
    conn.close()
    return [_row_to_voice_device(row) for row in rows]


def upsert_voice_device(
    device_id: str,
    friendly_name: Optional[str] = None,
    location_id: Optional[str] = None,
    output_speaker: str = "",
    fallback_speaker: Optional[str] = None,
    fallback_telegram: bool = True,
    fallback_local_speaker: bool = True,
    enabled: bool = True,
    notes: Optional[str] = None
) -> bool:
    """
    Crea o aggiorna un voice device.
    Se il device esiste, aggiorna solo i campi non-None passati.
    """
    device_id = device_id.upper()
    conn = _get_conn()
    c = conn.cursor()

    # Verifica se esiste già
    c.execute("SELECT device_id FROM voice_devices WHERE device_id = ?", (device_id,))
    exists = c.fetchone() is not None

    if exists:
        # Update - costruisci dinamicamente solo i campi da aggiornare
        updates = []
        params = []

        if friendly_name is not None:
            updates.append("friendly_name = ?")
            params.append(friendly_name)
        if location_id is not None:
            updates.append("location_id = ?")
            params.append(location_id)
        if output_speaker:
            updates.append("output_speaker = ?")
            params.append(output_speaker)
        if fallback_speaker is not None:
            updates.append("fallback_speaker = ?")
            params.append(fallback_speaker if fallback_speaker else None)

        updates.append("fallback_telegram = ?")
        params.append(fallback_telegram)
        updates.append("fallback_local_speaker = ?")
        params.append(fallback_local_speaker)
        updates.append("enabled = ?")
        params.append(enabled)

        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)

        params.append(device_id)
        c.execute(
            f"UPDATE voice_devices SET {', '.join(updates)} WHERE device_id = ?",
            params
        )
    else:
        # Insert nuovo device
        # Se location_id non è specificato, usa la prima location enabled come default
        if not location_id:
            c.execute("SELECT id FROM locations WHERE enabled = TRUE LIMIT 1")
            loc_row = c.fetchone()
            location_id = loc_row['id'] if loc_row else None

        c.execute("""
            INSERT INTO voice_devices (
                device_id, friendly_name, location_id, output_speaker,
                fallback_speaker, fallback_telegram, fallback_local_speaker,
                enabled, notes, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            device_id, friendly_name, location_id, output_speaker or "tts.speak",
            fallback_speaker, fallback_telegram, fallback_local_speaker,
            enabled, notes, time.time()
        ))

    conn.commit()
    conn.close()
    return True


def register_unknown_voice_device(
    device_id: str,
    firmware_version: Optional[str] = None,
    ip_address: Optional[str] = None
) -> VoiceDevice:
    """
    Registra un device sconosciuto per auto-discovery.
    Crea un record con friendly_name=NULL e valori di default.
    Ritorna il device (nuovo o esistente).
    """
    device_id = device_id.upper()
    conn = _get_conn()
    c = conn.cursor()

    # Verifica se esiste già
    c.execute("SELECT * FROM voice_devices WHERE device_id = ?", (device_id,))
    row = c.fetchone()

    if row:
        # Esiste già, aggiorna last_seen
        c.execute("""
            UPDATE voice_devices
            SET last_seen_at = ?, firmware_version = COALESCE(?, firmware_version),
                ip_address = COALESCE(?, ip_address)
            WHERE device_id = ?
        """, (time.time(), firmware_version, ip_address, device_id))
        conn.commit()
        # Rileggi il device aggiornato
        c.execute("SELECT * FROM voice_devices WHERE device_id = ?", (device_id,))
        row = c.fetchone()
    else:
        # Nuovo device - trova location di default
        c.execute("SELECT id FROM locations WHERE enabled = TRUE LIMIT 1")
        loc_row = c.fetchone()
        default_location = loc_row['id'] if loc_row else None

        c.execute("""
            INSERT INTO voice_devices (
                device_id, friendly_name, location_id, output_speaker,
                fallback_telegram, fallback_local_speaker, enabled,
                last_seen_at, firmware_version, ip_address
            ) VALUES (?, NULL, ?, 'tts.speak', 1, 1, 1, ?, ?, ?)
        """, (device_id, default_location, time.time(), firmware_version, ip_address))
        conn.commit()

        c.execute("SELECT * FROM voice_devices WHERE device_id = ?", (device_id,))
        row = c.fetchone()

    conn.close()
    return _row_to_voice_device(row)


def update_voice_device_heartbeat(
    device_id: str,
    firmware_version: Optional[str] = None,
    ip_address: Optional[str] = None
) -> Optional[VoiceDevice]:
    """
    Aggiorna last_seen_at e opzionalmente firmware/ip.
    Ritorna il device aggiornato o None se non esiste.
    """
    device_id = device_id.upper()
    conn = _get_conn()
    c = conn.cursor()

    c.execute("SELECT device_id FROM voice_devices WHERE device_id = ?", (device_id,))
    if not c.fetchone():
        conn.close()
        return None

    updates = ["last_seen_at = ?"]
    params = [time.time()]

    if firmware_version:
        updates.append("firmware_version = ?")
        params.append(firmware_version)
    if ip_address:
        updates.append("ip_address = ?")
        params.append(ip_address)

    params.append(device_id)
    c.execute(
        f"UPDATE voice_devices SET {', '.join(updates)} WHERE device_id = ?",
        params
    )
    conn.commit()

    c.execute("SELECT * FROM voice_devices WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    conn.close()

    return _row_to_voice_device(row) if row else None


def delete_voice_device(device_id: str) -> bool:
    """Elimina un voice device."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM voice_devices WHERE device_id = ?", (device_id.upper(),))
    conn.commit()
    affected = c.rowcount
    conn.close()
    return affected > 0


def get_voice_device_stats() -> Dict[str, Any]:
    """Statistiche sui voice devices."""
    conn = _get_conn()
    c = conn.cursor()

    # Totale devices
    c.execute("SELECT COUNT(*) as total FROM voice_devices")
    total = c.fetchone()['total']

    # Configurati
    c.execute("SELECT COUNT(*) as configured FROM voice_devices WHERE friendly_name IS NOT NULL")
    configured = c.fetchone()['configured']

    # Non configurati
    unconfigured = total - configured

    # Online (last_seen < 10 minuti)
    threshold = time.time() - 600
    c.execute("SELECT COUNT(*) as online FROM voice_devices WHERE last_seen_at > ?", (threshold,))
    online = c.fetchone()['online']

    # Enabled
    c.execute("SELECT COUNT(*) as enabled FROM voice_devices WHERE enabled = TRUE")
    enabled = c.fetchone()['enabled']

    conn.close()

    return {
        "total": total,
        "configured": configured,
        "unconfigured": unconfigured,
        "online": online,
        "offline": total - online,
        "enabled": enabled,
        "disabled": total - enabled
    }


# ===========================================================================
# USER MEMORY (stratificata: hot/warm/cold/longterm)
# ===========================================================================

def get_user_memory_context(
    user_id: int,
    include_hot: bool = True,
    include_warm: bool = True,
    include_cold: bool = True,
    include_longterm: bool = True,
    hot_minutes: int = 30,
    warm_hours: int = 24,
    cold_days: int = 7
) -> Dict[str, Any]:
    """
    Recupera memoria stratificata per un utente.
    Estende get_weighted_context() con i layer warm/cold/longterm.
    """
    conn = _get_conn()
    c = conn.cursor()

    result = {
        "hot": [],
        "warm": [],
        "cold": [],
        "longterm": [],
        "token_estimate": 0
    }

    now = time.time()

    # HOT: messaggi raw recenti (ultimi 30 min di chat_memory)
    if include_hot:
        cutoff = now - (hot_minutes * 60)
        c.execute('''
            SELECT role, content, speaker_name, timestamp
            FROM chat_memory
            WHERE timestamp > ? AND speaker_id = ?
            ORDER BY timestamp DESC
            LIMIT 20
        ''', (cutoff, user_id))
        result["hot"] = [dict(row) for row in c.fetchall()]

    # WARM: summaries orari (ultime 24h)
    if include_warm:
        cutoff = now - (warm_hours * 3600)
        c.execute('''
            SELECT hour_start, summary, message_count
            FROM user_memory_hourly
            WHERE user_id = ? AND hour_start > ?
            ORDER BY hour_start DESC
        ''', (user_id, cutoff))
        result["warm"] = [dict(row) for row in c.fetchall()]

    # COLD: summaries giornalieri (ultimi 7 giorni)
    if include_cold:
        c.execute('''
            SELECT date, summary, patterns_json
            FROM user_memory_daily
            WHERE user_id = ?
            ORDER BY date DESC
            LIMIT ?
        ''', (user_id, cold_days))
        result["cold"] = [dict(row) for row in c.fetchall()]

    # LONGTERM: fatti permanenti
    if include_longterm:
        c.execute('''
            SELECT fact, category, confidence
            FROM user_memory_longterm
            WHERE user_id = ?
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY confidence DESC, updated_at DESC
            LIMIT 20
        ''', (user_id, now))
        result["longterm"] = [dict(row) for row in c.fetchall()]

    conn.close()

    # Stima token (rough: 1 token ~ 4 chars)
    total_chars = sum(
        len(str(m.get("content", m.get("summary", m.get("fact", "")))))
        for layer in result.values() if isinstance(layer, list)
        for m in layer
    )
    result["token_estimate"] = total_chars // 4

    return result


def format_user_memory_for_llm(
    memory: Dict[str, Any],
    user_name: str,
    max_tokens: int = 2000
) -> str:
    """
    Formatta la memoria utente per il prompt.
    Rispetta il budget token, prioritizzando hot > longterm > warm > cold.
    """
    lines = []
    tokens_used = 0

    def add_if_budget(text: str) -> bool:
        nonlocal tokens_used
        est_tokens = len(text) // 4
        if tokens_used + est_tokens <= max_tokens:
            lines.append(text)
            tokens_used += est_tokens
            return True
        return False

    # LONGTERM prima (sempre utile, compatto)
    if memory["longterm"]:
        add_if_budget(f"\n[Informazioni su {user_name}]")
        for fact in memory["longterm"]:
            if not add_if_budget(f"- {fact['fact']}"):
                break

    # HOT (conversazione recente)
    if memory["hot"]:
        add_if_budget(f"\n[Conversazione recente con {user_name}]")
        for msg in reversed(memory["hot"][-10:]):
            prefix = user_name if msg['role'] == 'user' else 'Jarvis'
            if not add_if_budget(f"- {prefix}: {msg['content'][:200]}"):
                break

    # WARM (se c'e' budget)
    if memory["warm"] and tokens_used < max_tokens * 0.7:
        add_if_budget(f"\n[Attivita' ultime ore]")
        for summary in memory["warm"][:3]:
            if not add_if_budget(f"- {summary['summary'][:150]}"):
                break

    # COLD (se c'e' ancora budget)
    if memory["cold"] and tokens_used < max_tokens * 0.9:
        add_if_budget(f"\n[Pattern recenti]")
        for day in memory["cold"][:2]:
            if not add_if_budget(f"- {day['date']}: {day['summary'][:100]}"):
                break

    return "\n".join(lines)


def save_user_hourly_summary(
    user_id: int,
    hour_start: float,
    hour_end: float,
    summary: str,
    message_count: int
):
    """Salva summary orario per utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute('''
        INSERT INTO user_memory_hourly (user_id, hour_start, hour_end, summary, message_count)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, hour_start, hour_end, summary, message_count))
    conn.commit()
    conn.close()


def save_user_daily_summary(
    user_id: int,
    date: str,
    summary: str,
    patterns: dict,
    anomalies: list
):
    """Salva summary giornaliero per utente."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO user_memory_daily (user_id, date, summary, patterns_json, anomalies_json)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, date, summary, json.dumps(patterns), json.dumps(anomalies)))
    conn.commit()
    conn.close()


def save_user_longterm_fact(
    user_id: int,
    fact: str,
    category: str = "extracted",
    source: str = "auto",
    confidence: float = 0.8
):
    """Salva fatto long-term per utente."""
    conn = _get_conn()
    c = conn.cursor()

    # Check se esiste gia' un fatto identico
    c.execute('''
        SELECT id FROM user_memory_longterm
        WHERE user_id = ? AND fact = ?
    ''', (user_id, fact))

    if c.fetchone():
        # Aggiorna timestamp
        c.execute('''
            UPDATE user_memory_longterm SET updated_at = ?
            WHERE user_id = ? AND fact = ?
        ''', (time.time(), user_id, fact))
    else:
        # Inserisci nuovo
        c.execute('''
            INSERT INTO user_memory_longterm (user_id, fact, category, source, confidence)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, fact, category, source, confidence))

    conn.commit()
    conn.close()

    # Salva anche in vector store
    try:
        from vector_store import user_vector_store
        user_vector_store.add_user_fact(
            user_id=user_id,
            fact=fact,
            category=category,
            confidence=confidence,
            source=source
        )
    except Exception as e:
        logging.getLogger("JARVIS").warning(f"Vector store fact write failed: {e}")


def cleanup_old_user_memory(
    hourly_max_age_hours: int = 48,
    daily_max_age_days: int = 30
):
    """Pulisce memoria utente vecchia."""
    conn = _get_conn()
    c = conn.cursor()

    now = time.time()

    # Cleanup hourly > 48h
    cutoff_hourly = now - (hourly_max_age_hours * 3600)
    c.execute('DELETE FROM user_memory_hourly WHERE hour_start < ?', (cutoff_hourly,))

    # Cleanup daily > 30 giorni
    from datetime import datetime, timedelta
    cutoff_date = (datetime.now() - timedelta(days=daily_max_age_days)).strftime("%Y-%m-%d")
    c.execute('DELETE FROM user_memory_daily WHERE date < ?', (cutoff_date,))

    conn.commit()
    conn.close()


# ===========================================================================
# ACCESS LOG & AUTH ATTEMPTS (Security Logging)
# ===========================================================================

def log_access(
    method: str,
    path: str,
    status_code: int,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    user_id: Optional[int] = None,
    response_time_ms: Optional[float] = None
):
    """Registra una richiesta HTTP nell'access_log."""
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute(
            """INSERT INTO access_log
               (timestamp, method, path, status_code, ip_address, user_agent, user_id, response_time_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), method, path, status_code, ip_address,
             user_agent[:500] if user_agent else None,  # Tronca UA troppo lunghi
             user_id, response_time_ms)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Non bloccare mai il flusso principale per errori di logging


def log_auth_attempt(
    email: Optional[str],
    ip_address: Optional[str],
    user_agent: Optional[str],
    success: bool,
    failure_reason: Optional[str] = None
):
    """Registra un tentativo di autenticazione."""
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute(
            """INSERT INTO auth_attempts
               (timestamp, email, ip_address, user_agent, success, failure_reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (time.time(), email, ip_address,
             user_agent[:500] if user_agent else None,
             success, failure_reason)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_consecutive_failures(
    email: Optional[str] = None,
    ip_address: Optional[str] = None,
    window_seconds: int = 3600
) -> int:
    """
    Conta i tentativi falliti consecutivi per email o IP nell'ultima finestra temporale.
    Si ferma al primo successo (conta solo i fallimenti consecutivi recenti).
    """
    conn = _get_conn()
    c = conn.cursor()

    cutoff = time.time() - window_seconds
    conditions = ["timestamp > ?"]
    params: list = [cutoff]

    if email:
        conditions.append("email = ?")
        params.append(email)
    if ip_address:
        conditions.append("ip_address = ?")
        params.append(ip_address)

    where = " AND ".join(conditions)
    c.execute(
        f"SELECT success FROM auth_attempts WHERE {where} ORDER BY timestamp DESC",
        params
    )

    count = 0
    for row in c.fetchall():
        if row["success"]:
            break
        count += 1

    conn.close()
    return count


def get_access_log(
    limit: int = 100,
    offset: int = 0,
    ip: Optional[str] = None,
    status_code: Optional[int] = None,
    path_pattern: Optional[str] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    user_id: Optional[int] = None,
    method: Optional[str] = None
) -> Dict:
    """Recupera access log con filtri e paginazione."""
    conn = _get_conn()
    c = conn.cursor()

    conditions = []
    params: list = []

    if ip:
        conditions.append("ip_address = ?")
        params.append(ip)
    if status_code:
        conditions.append("status_code = ?")
        params.append(status_code)
    if path_pattern:
        conditions.append("path LIKE ?")
        params.append(f"%{path_pattern}%")
    if start_time:
        conditions.append("timestamp >= ?")
        params.append(start_time)
    if end_time:
        conditions.append("timestamp <= ?")
        params.append(end_time)
    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)
    if method:
        conditions.append("method = ?")
        params.append(method)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    # Conteggio totale
    c.execute(f"SELECT COUNT(*) as total FROM access_log{where}", params)
    total = c.fetchone()["total"]

    # Risultati paginati
    c.execute(
        f"""SELECT id, timestamp, method, path, status_code, ip_address,
                   user_agent, user_id, response_time_ms
            FROM access_log{where}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset]
    )

    items = []
    for row in c.fetchall():
        items.append({
            "id": row["id"],
            "timestamp": row["timestamp"],
            "method": row["method"],
            "path": row["path"],
            "status_code": row["status_code"],
            "ip_address": row["ip_address"],
            "user_agent": row["user_agent"],
            "user_id": row["user_id"],
            "response_time_ms": round(row["response_time_ms"], 1) if row["response_time_ms"] else None
        })

    conn.close()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_access_log_stats() -> Dict:
    """Statistiche aggregate dell'access log."""
    conn = _get_conn()
    c = conn.cursor()

    now = time.time()
    last_hour = now - 3600
    last_24h = now - 86400

    # Richieste nell'ultima ora e ultime 24h
    c.execute("SELECT COUNT(*) as cnt FROM access_log WHERE timestamp > ?", (last_hour,))
    requests_last_hour = c.fetchone()["cnt"]

    c.execute("SELECT COUNT(*) as cnt FROM access_log WHERE timestamp > ?", (last_24h,))
    requests_last_24h = c.fetchone()["cnt"]

    # Top IP (ultime 24h)
    c.execute("""
        SELECT ip_address, COUNT(*) as cnt
        FROM access_log WHERE timestamp > ? AND ip_address IS NOT NULL
        GROUP BY ip_address ORDER BY cnt DESC LIMIT 10
    """, (last_24h,))
    top_ips = [{"ip": row["ip_address"], "count": row["cnt"]} for row in c.fetchall()]

    # Top paths (ultime 24h)
    c.execute("""
        SELECT path, COUNT(*) as cnt
        FROM access_log WHERE timestamp > ?
        GROUP BY path ORDER BY cnt DESC LIMIT 10
    """, (last_24h,))
    top_paths = [{"path": row["path"], "count": row["cnt"]} for row in c.fetchall()]

    # Distribuzione status code (ultime 24h)
    c.execute("""
        SELECT status_code, COUNT(*) as cnt
        FROM access_log WHERE timestamp > ? AND status_code IS NOT NULL
        GROUP BY status_code ORDER BY cnt DESC
    """, (last_24h,))
    status_distribution = [{"status": row["status_code"], "count": row["cnt"]} for row in c.fetchall()]

    # Errori (4xx/5xx) ultime 24h
    c.execute("""
        SELECT COUNT(*) as cnt FROM access_log
        WHERE timestamp > ? AND status_code >= 400
    """, (last_24h,))
    errors_24h = c.fetchone()["cnt"]

    # Tempo medio risposta (ultime 24h)
    c.execute("""
        SELECT AVG(response_time_ms) as avg_ms
        FROM access_log WHERE timestamp > ? AND response_time_ms IS NOT NULL
    """, (last_24h,))
    avg_response = c.fetchone()["avg_ms"]

    conn.close()

    return {
        "requests_last_hour": requests_last_hour,
        "requests_last_24h": requests_last_24h,
        "errors_last_24h": errors_24h,
        "avg_response_ms": round(avg_response, 1) if avg_response else 0,
        "top_ips": top_ips,
        "top_paths": top_paths,
        "status_distribution": status_distribution
    }


def get_auth_attempts_log(
    limit: int = 50,
    email: Optional[str] = None,
    ip: Optional[str] = None,
    success_only: Optional[bool] = None
) -> List[Dict]:
    """Lista tentativi di autenticazione con filtri."""
    conn = _get_conn()
    c = conn.cursor()

    conditions = []
    params: list = []

    if email:
        conditions.append("email = ?")
        params.append(email)
    if ip:
        conditions.append("ip_address = ?")
        params.append(ip)
    if success_only is not None:
        conditions.append("success = ?")
        params.append(success_only)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    c.execute(
        f"""SELECT id, timestamp, email, ip_address, user_agent, success, failure_reason
            FROM auth_attempts{where}
            ORDER BY timestamp DESC LIMIT ?""",
        params + [limit]
    )

    items = []
    for row in c.fetchall():
        items.append({
            "id": row["id"],
            "timestamp": row["timestamp"],
            "email": row["email"],
            "ip_address": row["ip_address"],
            "user_agent": row["user_agent"],
            "success": bool(row["success"]),
            "failure_reason": row["failure_reason"]
        })

    conn.close()
    return items


def get_last_session_ip(user_id: int) -> Optional[str]:
    """Recupera l'IP dell'ultima sessione per un utente (esclusa la corrente)."""
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT ip_address FROM sessions
           WHERE user_id = ? AND ip_address IS NOT NULL
           ORDER BY created_at DESC LIMIT 1 OFFSET 1""",
        (user_id,)
    )
    row = c.fetchone()
    conn.close()
    return row["ip_address"] if row else None


def cleanup_old_access_logs(max_age_days: int = 30):
    """Pulizia access log e auth attempts vecchi."""
    conn = _get_conn()
    c = conn.cursor()
    cutoff = time.time() - (max_age_days * 86400)
    c.execute("DELETE FROM access_log WHERE timestamp < ?", (cutoff,))
    deleted_access = c.rowcount
    c.execute("DELETE FROM auth_attempts WHERE timestamp < ?", (cutoff,))
    deleted_auth = c.rowcount
    conn.commit()
    conn.close()
    if deleted_access or deleted_auth:
        logger.info(f"Cleanup: rimossi {deleted_access} access log, {deleted_auth} auth attempts (>{max_age_days}gg)")
