"""
JARVIS Image Generation API
- REST endpoint per generare immagini via Gemini
- Mostra su TV e/o invia a Telegram
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config

logger = logging.getLogger("JARVIS_IMAGE_API")

router = APIRouter(prefix="/api/image", tags=["image"])


class ImageGenerateRequest(BaseModel):
    """Request per generazione immagine."""
    prompt: str
    tv_entity: Optional[str] = None
    room: Optional[str] = None  # Alternative a tv_entity
    location_id: Optional[str] = None
    send_telegram: bool = False
    telegram_chat_id: Optional[int] = None


class ImageGenerateResponse(BaseModel):
    """Response della generazione immagine."""
    success: bool
    message: str
    image_url: Optional[str] = None


@router.post("/generate", response_model=ImageGenerateResponse)
async def api_generate_image(request: ImageGenerateRequest):
    """
    Endpoint per generare e mostrare immagini.

    Args:
        prompt: Descrizione dell'immagine da generare
        tv_entity: Entity ID della TV (es: media_player.tv_soggiorno)
        room: Alternativa a tv_entity - nome stanza (es: "soggiorno")
        location_id: ID della location HA
        send_telegram: Se inviare anche su Telegram
        telegram_chat_id: Chat ID Telegram (opzionale)

    Returns:
        JSON con success, message, image_url
    """
    # Check se Gemini è abilitato
    if not config.GEMINI_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Image generation requires GEMINI_API_KEY to be configured"
        )

    from image_generation import generate_and_show, extract_tv_from_room

    # Risolvi TV entity
    tv_entity = request.tv_entity
    if not tv_entity and request.room:
        tv_entity = extract_tv_from_room(request.room, request.location_id)

    # Se nessuna TV e non Telegram, fallback a Telegram
    send_telegram = request.send_telegram
    if not tv_entity and not send_telegram:
        logger.warning("No TV found and send_telegram=False, forcing Telegram")
        send_telegram = True

    # Genera immagine
    success, message, image_url = await generate_and_show(
        prompt=request.prompt,
        tv_entity=tv_entity,
        location_id=request.location_id,
        send_telegram=send_telegram,
        telegram_chat_id=request.telegram_chat_id
    )

    if not success:
        raise HTTPException(status_code=500, detail=message)

    return ImageGenerateResponse(
        success=success,
        message=message,
        image_url=image_url
    )


@router.get("/status")
async def api_image_status():
    """
    Verifica se la generazione immagini è disponibile.
    """
    return {
        "available": config.GEMINI_ENABLED,
        "model": config.GEMINI_IMAGE_MODEL if config.GEMINI_ENABLED else None,
        "images_dir": config.GENERATED_IMAGES_DIR
    }


@router.post("/cleanup")
async def api_cleanup_images(max_age_hours: int = 24):
    """
    Pulisce le immagini vecchie.

    Args:
        max_age_hours: Età massima in ore (default: 24)
    """
    from image_generation import cleanup_old_images

    await cleanup_old_images(max_age_hours)

    return {"status": "cleanup_completed", "max_age_hours": max_age_hours}
