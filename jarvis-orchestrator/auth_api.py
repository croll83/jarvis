"""
JARVIS Authentication API
- Login/Logout con sessioni
- Passkeys (WebAuthn) support
- Email OTP per reset password e verifica
- SMTP per invio email
"""

import os
import logging
import smtplib
import json
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from fastapi import APIRouter, Request, Response, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
import config

from database import (
    User, Session,
    get_user_by_email, get_user_by_id, get_user_by_name,
    create_user_with_auth, update_user_password, set_email_verified,
    verify_password, has_any_admin, needs_initial_setup, get_setup_status,
    create_session, get_session, delete_session, delete_user_sessions,
    get_user_sessions, cleanup_expired_sessions,
    create_otp_token, verify_otp_token, cleanup_expired_otp,
    save_passkey_credential, get_passkey_by_credential_id, get_user_passkeys,
    update_passkey_sign_count, delete_passkey,
    log_event,
    log_auth_attempt, get_consecutive_failures, get_last_session_ip
)

logger = logging.getLogger("JARVIS_AUTH")

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ===========================================================================
# CONFIGURATION (SMTP)
# ===========================================================================

SMTP_HOST = os.getenv("JARVIS_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("JARVIS_SMTP_PORT", "587"))
SMTP_USER = os.getenv("JARVIS_SMTP_USER", "")
SMTP_PASSWORD = os.getenv("JARVIS_SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("JARVIS_SMTP_FROM", "noreply@jarvis.local")
SMTP_USE_TLS = os.getenv("JARVIS_SMTP_TLS", "true").lower() == "true"

SESSION_COOKIE_NAME = "jarvis_session"
WEBAUTHN_RP_ID = os.getenv("JARVIS_WEBAUTHN_RP_ID", "localhost")
WEBAUTHN_RP_NAME = os.getenv("JARVIS_WEBAUTHN_RP_NAME", "JARVIS")
WEBAUTHN_ORIGIN = os.getenv("JARVIS_WEBAUTHN_ORIGIN", "http://localhost:8000")

# Challenge storage (in-memory, per simplicity - potrebbe andare in Redis)
_pending_challenges: dict = {}


# ===========================================================================
# MODELS
# ===========================================================================

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str


class SetupAdminRequest(BaseModel):
    name: str
    email: EmailStr
    password: str


class RequestOTPRequest(BaseModel):
    email: EmailStr
    purpose: str = "reset_password"  # or "verify_email"


class VerifyOTPRequest(BaseModel):
    email: EmailStr
    token: str
    purpose: str


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    token: str
    new_password: str


class PasskeyRegisterStart(BaseModel):
    device_name: str = "Unknown Device"


class PasskeyRegisterFinish(BaseModel):
    credential: dict
    device_name: str = "Unknown Device"


class PasskeyLoginStart(BaseModel):
    email: Optional[EmailStr] = None


class PasskeyLoginFinish(BaseModel):
    credential: dict


# ===========================================================================
# HELPERS
# ===========================================================================

def send_email(to: str, subject: str, body: str) -> bool:
    """Invia email via SMTP."""
    if not SMTP_HOST:
        logger.warning("SMTP not configured, cannot send email")
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_FROM
        msg['To'] = to
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)

        logger.info(f"Email sent to {to}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def get_current_session(request: Request) -> Optional[Session]:
    """Recupera la sessione corrente dal cookie."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    return get_session(session_id)


def get_current_user(request: Request) -> Optional[User]:
    """Recupera l'utente corrente dalla sessione."""
    session = get_current_session(request)
    if not session:
        return None
    return get_user_by_id(session.user_id)


def require_auth(request: Request) -> User:
    """Dependency per richiedere autenticazione."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(request: Request) -> User:
    """Dependency per richiedere admin."""
    user = require_auth(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    return user


def set_session_cookie(response: Response, session_id: str):
    """Imposta il cookie di sessione."""
    use_secure = os.getenv("JARVIS_SECURE_COOKIES", "auto")
    if use_secure == "auto":
        # Secure=True solo se c'è un dominio configurato (proxy SSL davanti)
        is_secure = bool(os.getenv("JARVIS_WEBAUTHN_RP_ID", "")) and os.getenv("JARVIS_WEBAUTHN_RP_ID") != "localhost"
    else:
        is_secure = use_secure.lower() in ("true", "1", "yes")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=config.SESSION_DURATION_SECONDS
    )


def clear_session_cookie(response: Response):
    """Rimuove il cookie di sessione."""
    response.delete_cookie(SESSION_COOKIE_NAME)


# ===========================================================================
# SETUP ENDPOINTS
# ===========================================================================

@router.get("/setup-status")
async def api_setup_status():
    """Ritorna lo stato del setup del sistema."""
    return get_setup_status()


@router.post("/setup")
async def api_setup_admin(data: SetupAdminRequest, request: Request, response: Response):
    """
    Setup iniziale: crea il primo admin.
    Funziona solo se non esistono admin nel sistema.
    """
    if has_any_admin():
        raise HTTPException(status_code=400, detail="Setup already completed")

    # Crea admin
    user_id = create_user_with_auth(
        name=data.name,
        email=data.email,
        password=data.password,
        role="admin"
    )

    if not user_id:
        raise HTTPException(status_code=400, detail="Failed to create admin (email may exist)")

    # Auto-login
    session_id = create_session(
        user_id=user_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent")
    )

    set_session_cookie(response, session_id)
    log_event("AUTH", f"Initial setup completed, admin created: {data.name}")

    return {"status": "ok", "user_id": user_id}


# ===========================================================================
# LOGIN/LOGOUT ENDPOINTS
# ===========================================================================

@router.post("/login")
async def api_login(data: LoginRequest, request: Request, response: Response):
    """Login con email e password."""
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "")
    user = get_user_by_email(data.email)

    if not user or not user.password_hash:
        log_auth_attempt(data.email, ip, ua, False, "user_not_found")
        consecutive = get_consecutive_failures(email=data.email)
        log_event("AUTH_FAIL", f"Login fallito: utente non trovato - {data.email} - IP: {ip} - Tentativi: {consecutive}")
        logger.warning(f"Login failed (user not found): email={data.email} ip={ip} attempts={consecutive}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(data.password, user.password_hash):
        log_auth_attempt(data.email, ip, ua, False, "wrong_password")
        consecutive = get_consecutive_failures(email=data.email)
        log_event("AUTH_FAIL", f"Login fallito: password errata - {data.email} - IP: {ip} - Tentativi: {consecutive}")
        logger.warning(f"Login failed (wrong password): email={data.email} ip={ip} attempts={consecutive}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Login riuscito
    log_auth_attempt(data.email, ip, ua, True)

    # Rileva cambio IP rispetto all'ultima sessione
    last_ip = get_last_session_ip(user.id)
    if last_ip and last_ip != ip:
        log_event("AUTH", f"Cambio IP per {user.name}: {last_ip} -> {ip}")
        logger.info(f"IP change detected for {user.name}: {last_ip} -> {ip}")

    # Crea sessione
    session_id = create_session(
        user_id=user.id,
        ip_address=ip,
        user_agent=ua
    )

    set_session_cookie(response, session_id)
    log_event("AUTH", f"Login successful: {user.name} - IP: {ip}")

    return {"status": "ok", "user": user.to_dict()}


@router.post("/logout")
async def api_logout(request: Request, response: Response):
    """Logout - invalida la sessione corrente."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        delete_session(session_id)

    clear_session_cookie(response)
    return {"status": "ok"}


@router.post("/logout-all")
async def api_logout_all(request: Request, response: Response, user: User = Depends(require_auth)):
    """Logout da tutte le sessioni."""
    count = delete_user_sessions(user.id)
    clear_session_cookie(response)
    log_event("AUTH", f"Logged out from all sessions: {user.name} ({count} sessions)")
    return {"status": "ok", "sessions_invalidated": count}


@router.get("/me")
async def api_get_current_user(user: User = Depends(require_auth)):
    """Ritorna l'utente corrente."""
    return user.to_dict()


@router.get("/sessions")
async def api_get_sessions(user: User = Depends(require_auth)):
    """Ritorna le sessioni attive dell'utente."""
    sessions = get_user_sessions(user.id)
    return [{
        "id": s.id[:8] + "...",  # Troncato per sicurezza
        "created_at": s.created_at,
        "expires_at": s.expires_at,
        "ip_address": s.ip_address,
        "user_agent": s.user_agent
    } for s in sessions]


# ===========================================================================
# OTP / PASSWORD RESET
# ===========================================================================

@router.post("/request-otp")
async def api_request_otp(data: RequestOTPRequest):
    """Richiede un OTP via email."""
    user = get_user_by_email(data.email)

    # Per sicurezza, rispondiamo sempre OK anche se l'email non esiste
    if not user:
        return {"status": "ok", "message": "If the email exists, an OTP was sent"}

    token = create_otp_token(data.email, data.purpose, user.id)

    # Costruisci email
    if data.purpose == "reset_password":
        subject = "JARVIS - Reset Password"
        body = f"""
        <h2>Reset Password</h2>
        <p>Hai richiesto il reset della password per JARVIS.</p>
        <p>Il tuo codice di verifica è:</p>
        <h1 style="font-family: monospace; letter-spacing: 3px;">{token}</h1>
        <p>Questo codice scade tra 15 minuti.</p>
        <p>Se non hai richiesto questo reset, ignora questa email.</p>
        """
    else:
        subject = "JARVIS - Verifica Email"
        body = f"""
        <h2>Verifica Email</h2>
        <p>Il tuo codice di verifica è:</p>
        <h1 style="font-family: monospace; letter-spacing: 3px;">{token}</h1>
        <p>Questo codice scade tra 15 minuti.</p>
        """

    sent = send_email(data.email, subject, body)

    if not sent:
        logger.warning(f"OTP created but email not sent (SMTP not configured?): {data.email}")

    return {"status": "ok", "message": "If the email exists, an OTP was sent"}


@router.post("/verify-otp")
async def api_verify_otp(data: VerifyOTPRequest):
    """Verifica un OTP."""
    user_id = verify_otp_token(data.token, data.email, data.purpose)

    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    if data.purpose == "verify_email":
        set_email_verified(user_id, True)
        log_event("AUTH", f"Email verified for user {user_id}")

    return {"status": "ok", "user_id": user_id}


@router.post("/reset-password")
async def api_reset_password(data: ResetPasswordRequest):
    """Reset password con OTP."""
    user_id = verify_otp_token(data.token, data.email, "reset_password")

    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    update_user_password(user_id, data.new_password)
    delete_user_sessions(user_id)  # Invalida tutte le sessioni

    log_event("AUTH", f"Password reset for user {user_id}")

    return {"status": "ok"}


# ===========================================================================
# PASSKEYS (WebAuthn)
# ===========================================================================

try:
    from webauthn import (
        generate_registration_options,
        verify_registration_response,
        generate_authentication_options,
        verify_authentication_response,
        options_to_json
    )
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
        AuthenticatorSelectionCriteria,
        ResidentKeyRequirement
    )
    WEBAUTHN_AVAILABLE = True
except ImportError:
    WEBAUTHN_AVAILABLE = False
    logger.warning("webauthn package not installed - passkeys disabled")


@router.get("/passkeys/available")
async def api_passkeys_available():
    """Controlla se le passkeys sono disponibili."""
    return {"available": WEBAUTHN_AVAILABLE}


@router.post("/passkeys/register/start")
async def api_passkey_register_start(
    data: PasskeyRegisterStart,
    user: User = Depends(require_auth)
):
    """Inizia la registrazione di una passkey."""
    if not WEBAUTHN_AVAILABLE:
        raise HTTPException(status_code=501, detail="Passkeys not available")

    # Recupera passkeys esistenti
    existing = get_user_passkeys(user.id)
    exclude_credentials = [
        PublicKeyCredentialDescriptor(id=pk.credential_id)
        for pk in existing
    ]

    options = generate_registration_options(
        rp_id=WEBAUTHN_RP_ID,
        rp_name=WEBAUTHN_RP_NAME,
        user_id=str(user.id).encode(),
        user_name=user.email or user.name,
        user_display_name=user.name,
        exclude_credentials=exclude_credentials,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED
        )
    )

    # Salva challenge per verifica
    _pending_challenges[f"reg_{user.id}"] = options.challenge

    return JSONResponse(content=json.loads(options_to_json(options)))


@router.post("/passkeys/register/finish")
async def api_passkey_register_finish(
    data: PasskeyRegisterFinish,
    user: User = Depends(require_auth)
):
    """Completa la registrazione di una passkey."""
    if not WEBAUTHN_AVAILABLE:
        raise HTTPException(status_code=501, detail="Passkeys not available")

    challenge = _pending_challenges.pop(f"reg_{user.id}", None)
    if not challenge:
        raise HTTPException(status_code=400, detail="No pending registration")

    try:
        verification = verify_registration_response(
            credential=data.credential,
            expected_challenge=challenge,
            expected_rp_id=WEBAUTHN_RP_ID,
            expected_origin=WEBAUTHN_ORIGIN
        )

        pk_id = save_passkey_credential(
            user_id=user.id,
            credential_id=verification.credential_id,
            public_key=verification.credential_public_key,
            device_name=data.device_name
        )

        log_event("AUTH", f"Passkey registered for {user.name}: {data.device_name}")

        return {"status": "ok", "passkey_id": pk_id}

    except Exception as e:
        logger.error(f"Passkey registration failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/passkeys/login/start")
async def api_passkey_login_start(data: PasskeyLoginStart):
    """Inizia il login con passkey."""
    if not WEBAUTHN_AVAILABLE:
        raise HTTPException(status_code=501, detail="Passkeys not available")

    allow_credentials = []

    if data.email:
        user = get_user_by_email(data.email)
        if user:
            passkeys = get_user_passkeys(user.id)
            allow_credentials = [
                PublicKeyCredentialDescriptor(id=pk.credential_id)
                for pk in passkeys
            ]

    options = generate_authentication_options(
        rp_id=WEBAUTHN_RP_ID,
        allow_credentials=allow_credentials if allow_credentials else None,
        user_verification=UserVerificationRequirement.PREFERRED
    )

    # Salva challenge
    challenge_key = f"auth_{data.email or 'discoverable'}"
    _pending_challenges[challenge_key] = {
        "challenge": options.challenge,
        "email": data.email
    }

    return JSONResponse(content=json.loads(options_to_json(options)))


@router.post("/passkeys/login/finish")
async def api_passkey_login_finish(
    data: PasskeyLoginFinish,
    request: Request,
    response: Response
):
    """Completa il login con passkey."""
    if not WEBAUTHN_AVAILABLE:
        raise HTTPException(status_code=501, detail="Passkeys not available")

    # Trova la challenge (potrebbe essere per email specifica o discoverable)
    credential_id = base64.urlsafe_b64decode(
        data.credential.get("rawId", "") + "=="
    )

    passkey = get_passkey_by_credential_id(credential_id)
    if not passkey:
        raise HTTPException(status_code=401, detail="Unknown passkey")

    user = get_user_by_id(passkey.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Trova challenge
    challenge_key = f"auth_{user.email or 'discoverable'}"
    challenge_data = _pending_challenges.pop(challenge_key, None)

    # Prova anche discoverable
    if not challenge_data:
        challenge_data = _pending_challenges.pop("auth_discoverable", None)

    if not challenge_data:
        raise HTTPException(status_code=400, detail="No pending authentication")

    try:
        verification = verify_authentication_response(
            credential=data.credential,
            expected_challenge=challenge_data["challenge"],
            expected_rp_id=WEBAUTHN_RP_ID,
            expected_origin=WEBAUTHN_ORIGIN,
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count
        )

        # Aggiorna sign count
        update_passkey_sign_count(credential_id, verification.new_sign_count)

        # Crea sessione
        session_id = create_session(
            user_id=user.id,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent")
        )

        set_session_cookie(response, session_id)
        log_event("AUTH", f"Passkey login: {user.name}")

        return {"status": "ok", "user": user.to_dict()}

    except Exception as e:
        logger.error(f"Passkey authentication failed: {e}")
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/passkeys")
async def api_get_passkeys(user: User = Depends(require_auth)):
    """Lista le passkey dell'utente."""
    passkeys = get_user_passkeys(user.id)
    return [{
        "id": pk.id,
        "device_name": pk.device_name,
        "created_at": pk.created_at,
        "last_used": pk.last_used
    } for pk in passkeys]


@router.delete("/passkeys/{pk_id}")
async def api_delete_passkey(pk_id: str, user: User = Depends(require_auth)):
    """Elimina una passkey."""
    if not delete_passkey(pk_id, user.id):
        raise HTTPException(status_code=404, detail="Passkey not found")

    log_event("AUTH", f"Passkey deleted: {pk_id}")
    return {"status": "ok"}


# ===========================================================================
# MAINTENANCE
# ===========================================================================

@router.post("/cleanup")
async def api_cleanup(user: User = Depends(require_admin)):
    """Pulizia sessioni e OTP scaduti."""
    sessions = cleanup_expired_sessions()
    otps = cleanup_expired_otp()
    return {"sessions_cleaned": sessions, "otps_cleaned": otps}
