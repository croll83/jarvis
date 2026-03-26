"""
JARVIS Backup & Restore Module
- Backup completo: SQLite DB + ChromaDB + Voice Models
- Validazione archivio prima del restore
- Safety backup automatico prima del ripristino
"""

import os
import json
import time
import shutil
import sqlite3
import tarfile
import tempfile
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Callable, List

import config
from database import DB_PATH

logger = logging.getLogger("JARVIS_BACKUP")

# Paths dei componenti da includere nel backup
CHROMA_PATH = Path(config.CHROMA_PATH)
VOICE_MODELS_DIR = Path(config.VOICE_MODELS_DIR)
BACKUP_VERSION = "1.0"

# Directory temporanea per i backup
BACKUP_TMP_DIR = Path(tempfile.gettempdir()) / "jarvis_backups"


def _get_dir_size(path: Path) -> int:
    """Calcola dimensione totale di una directory in bytes."""
    total = 0
    if path.exists() and path.is_dir():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total


def _get_db_tables() -> List[str]:
    """Elenca le tabelle nel database SQLite."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        return tables
    except Exception:
        return []


def _get_table_counts() -> Dict[str, int]:
    """Conta le righe per ogni tabella del database."""
    counts = {}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        for table in _get_db_tables():
            try:
                cursor = conn.execute(f"SELECT COUNT(*) FROM [{table}]")
                counts[table] = cursor.fetchone()[0]
            except Exception:
                counts[table] = -1
        conn.close()
    except Exception:
        pass
    return counts


def create_backup(output_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Crea un backup completo del sistema JARVIS.

    Include:
    - SQLite database (backup API nativa, sicura durante scritture)
    - ChromaDB directory (vector store)
    - Voice Models directory (modelli speaker)
    - metadata.json (versione, timestamp, manifest)

    Returns:
        Dict con: path, size_bytes, metadata, components
    """
    BACKUP_TMP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    if output_path:
        archive_path = Path(output_path)
    else:
        archive_path = BACKUP_TMP_DIR / f"jarvis_backup_{timestamp}.tar.gz"

    # Directory temporanea per assemblare il backup
    staging_dir = Path(tempfile.mkdtemp(prefix="jarvis_backup_staging_"))
    components = {}

    try:
        # 1. SQLite Database - usa backup API nativa (sicura anche durante scritture)
        if DB_PATH.exists():
            db_backup_path = staging_dir / "jarvis_state.db"
            src_conn = sqlite3.connect(str(DB_PATH))
            dst_conn = sqlite3.connect(str(db_backup_path))
            src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()
            components["database"] = {
                "status": "ok",
                "file": "jarvis_state.db",
                "size_bytes": db_backup_path.stat().st_size,
                "tables": _get_db_tables(),
                "table_counts": _get_table_counts()
            }
            logger.info(f"DB backup completato: {db_backup_path.stat().st_size} bytes")
        else:
            components["database"] = {"status": "missing", "error": f"{DB_PATH} not found"}
            logger.warning(f"Database non trovato: {DB_PATH}")

        # 2. ChromaDB - copia directory
        if CHROMA_PATH.exists() and CHROMA_PATH.is_dir():
            chroma_backup = staging_dir / "chroma"
            shutil.copytree(str(CHROMA_PATH), str(chroma_backup))
            chroma_size = _get_dir_size(chroma_backup)
            components["chromadb"] = {
                "status": "ok",
                "directory": "chroma/",
                "size_bytes": chroma_size,
                "file_count": sum(1 for _ in chroma_backup.rglob("*") if _.is_file())
            }
            logger.info(f"ChromaDB backup completato: {chroma_size} bytes")
        else:
            components["chromadb"] = {"status": "missing", "error": f"{CHROMA_PATH} not found"}
            logger.warning(f"ChromaDB non trovato: {CHROMA_PATH}")

        # 3. Voice Models - copia directory
        if VOICE_MODELS_DIR.exists() and VOICE_MODELS_DIR.is_dir():
            voice_backup = staging_dir / "voice_models"
            shutil.copytree(str(VOICE_MODELS_DIR), str(voice_backup))
            voice_size = _get_dir_size(voice_backup)
            components["voice_models"] = {
                "status": "ok",
                "directory": "voice_models/",
                "size_bytes": voice_size,
                "file_count": sum(1 for _ in voice_backup.rglob("*") if _.is_file())
            }
            logger.info(f"Voice models backup completato: {voice_size} bytes")
        else:
            components["voice_models"] = {"status": "missing", "error": f"{VOICE_MODELS_DIR} not found"}
            logger.info(f"Voice models non trovato (opzionale): {VOICE_MODELS_DIR}")

        # 4. Metadata
        metadata = {
            "version": BACKUP_VERSION,
            "timestamp": time.time(),
            "timestamp_human": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "created_by": "JARVIS Admin Dashboard",
            "components": components
        }
        metadata_path = staging_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False))

        # 5. Crea archivio tar.gz
        with tarfile.open(str(archive_path), "w:gz") as tar:
            for item in staging_dir.iterdir():
                tar.add(str(item), arcname=item.name)

        archive_size = archive_path.stat().st_size
        logger.info(f"Backup creato: {archive_path} ({archive_size} bytes)")

        return {
            "success": True,
            "path": str(archive_path),
            "size_bytes": archive_size,
            "metadata": metadata,
            "components": components
        }

    except Exception as e:
        logger.error(f"Errore durante il backup: {e}")
        # Pulizia archivio parziale
        if archive_path.exists():
            archive_path.unlink()
        return {
            "success": False,
            "error": str(e),
            "components": components
        }
    finally:
        # Pulizia staging directory
        shutil.rmtree(staging_dir, ignore_errors=True)


def validate_backup(archive_path: str) -> Dict[str, Any]:
    """
    Valida un archivio di backup senza estrarlo.

    Verifica:
    - Formato tar.gz valido
    - Presenza di metadata.json
    - Presenza di jarvis_state.db
    - Versione compatibile

    Returns:
        Dict con: valid, errors, warnings, metadata
    """
    errors = []
    warnings = []
    metadata = None

    try:
        if not Path(archive_path).exists():
            return {"valid": False, "errors": ["File non trovato"], "warnings": [], "metadata": None}

        if not tarfile.is_tarfile(archive_path):
            return {"valid": False, "errors": ["Non è un archivio tar valido"], "warnings": [], "metadata": None}

        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getnames()

            # Verifica metadata.json
            if "metadata.json" not in members:
                errors.append("metadata.json mancante nell'archivio")
            else:
                try:
                    f = tar.extractfile("metadata.json")
                    if f:
                        metadata = json.loads(f.read().decode("utf-8"))
                        # Verifica versione
                        version = metadata.get("version", "unknown")
                        if version != BACKUP_VERSION:
                            warnings.append(f"Versione backup ({version}) diversa da quella corrente ({BACKUP_VERSION})")
                except Exception as e:
                    errors.append(f"metadata.json non leggibile: {e}")

            # Verifica database
            if "jarvis_state.db" not in members:
                errors.append("jarvis_state.db mancante nell'archivio")
            else:
                # Verifica che sia un SQLite valido
                try:
                    f = tar.extractfile("jarvis_state.db")
                    if f:
                        header = f.read(16)
                        if not header.startswith(b"SQLite format 3"):
                            errors.append("jarvis_state.db non è un database SQLite valido")
                except Exception as e:
                    errors.append(f"jarvis_state.db non leggibile: {e}")

            # Verifica ChromaDB (opzionale ma consigliato)
            has_chroma = any(m.startswith("chroma/") for m in members)
            if not has_chroma:
                warnings.append("ChromaDB non incluso nel backup (i vettori andranno persi)")

            # Verifica Voice Models (opzionale)
            has_voice = any(m.startswith("voice_models/") for m in members)
            if not has_voice:
                warnings.append("Voice models non inclusi nel backup")

            # Calcola dimensioni
            total_size = sum(m.size for m in tar.getmembers() if m.isfile())

    except Exception as e:
        errors.append(f"Errore apertura archivio: {e}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "metadata": metadata,
        "total_uncompressed_bytes": total_size if 'total_size' in dir() else None
    }


def restore_backup(
    archive_path: str,
    stop_callback: Optional[Callable] = None,
    restart_callback: Optional[Callable] = None
) -> Dict[str, Any]:
    """
    Ripristina un backup completo del sistema.

    Passi:
    1. Valida l'archivio
    2. Crea safety backup dello stato corrente
    3. Ferma task in background (stop_callback)
    4. Estrae e sovrascrive: DB, ChromaDB, Voice Models
    5. Riavvia servizi (restart_callback)

    Returns:
        Dict con: success, steps_completed, safety_backup_path, errors
    """
    steps_completed = []
    errors = []

    # Step 1: Validazione
    validation = validate_backup(archive_path)
    if not validation["valid"]:
        return {
            "success": False,
            "errors": validation["errors"],
            "steps_completed": [],
            "safety_backup_path": None
        }
    steps_completed.append("validation_passed")

    # Step 2: Safety backup dello stato corrente
    safety_backup_path = None
    try:
        BACKUP_TMP_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        safety_path = str(BACKUP_TMP_DIR / f"jarvis_pre_restore_{timestamp}.tar.gz")
        safety_result = create_backup(output_path=safety_path)
        if safety_result["success"]:
            safety_backup_path = safety_result["path"]
            steps_completed.append("safety_backup_created")
            logger.info(f"Safety backup creato: {safety_backup_path}")
        else:
            errors.append(f"Safety backup fallito: {safety_result.get('error', 'unknown')}")
            # Continuiamo comunque, l'utente ha già confermato
            steps_completed.append("safety_backup_failed_continuing")
    except Exception as e:
        errors.append(f"Safety backup error: {e}")
        steps_completed.append("safety_backup_error_continuing")

    # Step 3: Ferma task in background
    if stop_callback:
        try:
            stop_callback()
            steps_completed.append("background_tasks_stopped")
        except Exception as e:
            errors.append(f"Stop callback error: {e}")

    # Step 4: Estrai e ripristina
    extract_dir = Path(tempfile.mkdtemp(prefix="jarvis_restore_"))
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            # Sicurezza: verifica che non ci siano path traversal
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name:
                    raise ValueError(f"Path non sicuro nell'archivio: {member.name}")
            tar.extractall(str(extract_dir))

        # 4a. Ripristina database
        restored_db = extract_dir / "jarvis_state.db"
        if restored_db.exists():
            # Copia atomica: scrivi su file temporaneo poi rinomina
            tmp_db = DB_PATH.with_suffix(".db.restoring")
            shutil.copy2(str(restored_db), str(tmp_db))
            tmp_db.replace(DB_PATH)
            steps_completed.append("database_restored")
            logger.info("Database ripristinato")
        else:
            errors.append("jarvis_state.db non trovato nell'archivio estratto")

        # 4b. Ripristina ChromaDB
        restored_chroma = extract_dir / "chroma"
        if restored_chroma.exists() and restored_chroma.is_dir():
            if CHROMA_PATH.exists():
                shutil.rmtree(str(CHROMA_PATH))
            shutil.copytree(str(restored_chroma), str(CHROMA_PATH))
            steps_completed.append("chromadb_restored")
            logger.info("ChromaDB ripristinato")
        else:
            steps_completed.append("chromadb_skipped_not_in_backup")

        # 4c. Ripristina Voice Models
        restored_voice = extract_dir / "voice_models"
        if restored_voice.exists() and restored_voice.is_dir():
            if VOICE_MODELS_DIR.exists():
                shutil.rmtree(str(VOICE_MODELS_DIR))
            shutil.copytree(str(restored_voice), str(VOICE_MODELS_DIR))
            steps_completed.append("voice_models_restored")
            logger.info("Voice models ripristinati")
        else:
            steps_completed.append("voice_models_skipped_not_in_backup")

    except Exception as e:
        logger.error(f"Errore durante il restore: {e}")
        errors.append(f"Restore error: {e}")
        return {
            "success": False,
            "errors": errors,
            "steps_completed": steps_completed,
            "safety_backup_path": safety_backup_path
        }
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)

    # Step 5: Riavvia servizi
    if restart_callback:
        try:
            restart_callback()
            steps_completed.append("services_restarted")
        except Exception as e:
            errors.append(f"Restart callback error: {e}")

    success = "database_restored" in steps_completed
    logger.info(f"Restore completato. Success={success}, Steps={steps_completed}")

    return {
        "success": success,
        "steps_completed": steps_completed,
        "errors": errors,
        "safety_backup_path": safety_backup_path,
        "metadata": validation.get("metadata")
    }


def get_backup_info() -> Dict[str, Any]:
    """
    Ritorna info sui componenti che verrebbero inclusi in un backup.
    Utile per la UI della dashboard.
    """
    info = {
        "database": {
            "exists": DB_PATH.exists(),
            "size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
            "tables": _get_db_tables(),
            "table_counts": _get_table_counts()
        },
        "chromadb": {
            "exists": CHROMA_PATH.exists() and CHROMA_PATH.is_dir(),
            "size_bytes": _get_dir_size(CHROMA_PATH),
            "path": str(CHROMA_PATH)
        },
        "voice_models": {
            "exists": VOICE_MODELS_DIR.exists() and VOICE_MODELS_DIR.is_dir(),
            "size_bytes": _get_dir_size(VOICE_MODELS_DIR),
            "path": str(VOICE_MODELS_DIR),
            "model_count": len(list(VOICE_MODELS_DIR.glob("*.npy"))) if VOICE_MODELS_DIR.exists() else 0
        }
    }

    # Totale stimato
    info["total_estimated_bytes"] = sum(
        c.get("size_bytes", 0) for c in info.values()
    )

    return info
