"""
JARVIS User Management API
- CRUD utenti
- Voice enrollment
- Interfaccia web per gestione
"""

from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from pydantic import BaseModel
from typing import Optional, List
import logging

from database import (
    create_user, get_user_by_id, get_all_users,
    update_user_role, delete_user, needs_initial_setup
)
from voice_recognition import voice_recognizer
from auth_api import get_current_user, require_admin, require_auth
from fastapi import Depends

logger = logging.getLogger("JARVIS_USER_API")

# User management API richiede admin
router = APIRouter(
    prefix="/api/users",
    tags=["users"],
    dependencies=[Depends(require_admin)]
)


# ===========================================================================
# PYDANTIC MODELS
# ===========================================================================

class UserCreate(BaseModel):
    name: str
    role: str = "user"  # "admin", "user", "guest"


class UserUpdate(BaseModel):
    role: Optional[str] = None


class EnrollmentSample(BaseModel):
    audio_base64: str  # Audio in base64


# ===========================================================================
# USER CRUD ENDPOINTS
# ===========================================================================

@router.get("/")
async def list_users() -> List[dict]:
    """Lista tutti gli utenti."""
    users = get_all_users()
    return [u.to_dict() for u in users]


@router.post("/")
async def create_new_user(user_data: UserCreate) -> dict:
    """Crea un nuovo utente."""
    if user_data.role not in ["admin", "user", "guest"]:
        raise HTTPException(400, "Role must be 'admin', 'user', or 'guest'")

    user_id = create_user(user_data.name, user_data.role)
    if user_id is None:
        raise HTTPException(400, f"User '{user_data.name}' already exists")

    user = get_user_by_id(user_id)
    return user.to_dict()


@router.get("/{user_id}")
async def get_user(user_id: int) -> dict:
    """Recupera un utente per ID."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")
    return user.to_dict()


@router.patch("/{user_id}")
async def update_user(user_id: int, user_data: UserUpdate) -> dict:
    """Aggiorna un utente."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    if user_data.role:
        if user_data.role not in ["admin", "user", "guest"]:
            raise HTTPException(400, "Role must be 'admin', 'user', or 'guest'")
        update_user_role(user_id, user_data.role)

    return get_user_by_id(user_id).to_dict()


@router.delete("/{user_id}")
async def remove_user(user_id: int) -> dict:
    """Elimina un utente."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    success = delete_user(user_id)
    if not success:
        raise HTTPException(400, "Cannot delete the last admin")

    # Elimina anche il modello vocale
    voice_recognizer.delete_voice_model(user_id)

    return {"status": "deleted", "user_id": user_id}


# ===========================================================================
# VOICE ENROLLMENT ENDPOINTS
# ===========================================================================

@router.post("/{user_id}/voice/start")
async def start_voice_enrollment(user_id: int) -> dict:
    """Inizia l'enrollment vocale per un utente."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    return voice_recognizer.start_enrollment(user_id)


@router.post("/{user_id}/voice/sample")
async def add_voice_sample(user_id: int, audio: UploadFile = File(...)) -> dict:
    """Aggiunge un campione vocale all'enrollment."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    audio_bytes = await audio.read()
    return voice_recognizer.add_enrollment_sample(user_id, audio_bytes)


@router.post("/{user_id}/voice/complete")
async def complete_voice_enrollment(user_id: int) -> dict:
    """Completa l'enrollment vocale."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    return voice_recognizer.complete_enrollment(user_id)


@router.post("/{user_id}/voice/cancel")
async def cancel_voice_enrollment(user_id: int) -> dict:
    """Annulla l'enrollment vocale in corso."""
    return voice_recognizer.cancel_enrollment(user_id)


@router.delete("/{user_id}/voice")
async def delete_voice_model(user_id: int) -> dict:
    """Elimina il modello vocale di un utente."""
    return voice_recognizer.delete_voice_model(user_id)


@router.get("/{user_id}/voice/status")
async def get_voice_status(user_id: int) -> dict:
    """Ritorna lo stato dell'enrollment vocale."""
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    return voice_recognizer.get_enrollment_status(user_id)


@router.post("/{user_id}/voice/quick-sample")
async def quick_voice_sample(user_id: int, audio: UploadFile = File(...)) -> dict:
    """
    Aggiunge un campione di enrollment rapido da upload audio.
    Skippa audio < 0.5s. Auto-completa quando raggiunge MIN_ENROLLMENT_SAMPLES.
    Utile per enrollment da AtomS3R o altri device remoti.
    """
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    audio_bytes = await audio.read()
    return voice_recognizer.quick_enroll_from_session(user_id, audio_bytes)


@router.post("/{user_id}/voice/re-enroll")
async def re_enroll_voice(user_id: int) -> dict:
    """
    Cancella il modello vocale esistente e avvia un nuovo enrollment.
    Utile per cambio microfono (es. da Mac a AtomS3R).
    Dopo questa chiamata, l'utente può semplicemente parlare all'AtomS3R
    e i campioni vengono raccolti automaticamente da _process_ws_audio().
    """
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    # Cancella modello esistente
    voice_recognizer.delete_voice_model(user_id)

    # Avvia nuovo enrollment
    result = voice_recognizer.start_enrollment(user_id)
    result["re_enroll"] = True
    logger.info(f"Re-enrollment started for user {user_id} ({user.name})")
    return result


@router.post("/voice/test")
async def test_voice_identification(audio: UploadFile = File(...)) -> dict:
    """
    Testa l'identificazione vocale senza salvare.
    Utile per verificare che il sistema riconosca correttamente un utente.
    """
    audio_bytes = await audio.read()
    result = voice_recognizer.identify_speaker(audio_bytes)

    return {
        "identified": result.is_known,
        "speaker_name": result.user_name or "Sconosciuto",
        "speaker_id": result.user_id,
        "confidence": result.confidence,
    }


# ===========================================================================
# WEB INTERFACE
# ===========================================================================

web_router = APIRouter(tags=["web"])

# Path to templates directory
TEMPLATES_DIR = Path(__file__).parent / "templates"


@web_router.get("/")
async def root_redirect(request: Request):
    """Redirect dalla root alla dashboard o login."""
    if needs_initial_setup():
        return RedirectResponse(url="/login", status_code=302)

    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/admin", status_code=302)
    else:
        return RedirectResponse(url="/login", status_code=302)


@web_router.get("/login", response_class=HTMLResponse)
async def login_page():
    """Pagina di login."""
    template_path = TEMPLATES_DIR / "login.html"
    if template_path.exists():
        return HTMLResponse(content=template_path.read_text(encoding="utf-8"))
    else:
        return HTMLResponse(content="<h1>Login template not found</h1>", status_code=500)


@web_router.get("/admin", response_class=HTMLResponse)
async def admin_interface(request: Request):
    """Interfaccia web admin completa - richiede autenticazione."""
    # Se serve setup iniziale, redirect a login
    if needs_initial_setup():
        return RedirectResponse(url="/login", status_code=302)

    # Verifica autenticazione
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    template_path = TEMPLATES_DIR / "admin_dashboard.html"
    if template_path.exists():
        return HTMLResponse(content=template_path.read_text(encoding="utf-8"))
    else:
        return HTMLResponse(content="<h1>Template not found</h1>", status_code=500)


@web_router.get("/assets/{filename}")
async def serve_asset(filename: str):
    """Serve file statici dalla cartella assets."""
    assets_dir = TEMPLATES_DIR / "assets"
    file_path = assets_dir / filename

    if not file_path.exists():
        raise HTTPException(404, "Asset not found")

    # Determina il content type
    suffix = file_path.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".webp": "image/webp",
    }
    media_type = media_types.get(suffix, "application/octet-stream")

    return FileResponse(file_path, media_type=media_type)
