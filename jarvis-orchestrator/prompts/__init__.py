"""
JARVIS Prompt Templates Loader

I prompt sono in file .txt per facilitare traduzione e modifica.
Ogni file usa placeholder Python standard: {variable_name}
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("JARVIS_PROMPTS")

_PROMPTS_DIR = Path(__file__).parent
_cache: dict[str, str] = {}


def load_prompt(name: str, fallback: Optional[str] = None) -> str:
    """
    Carica un prompt template da file .txt.

    Args:
        name: Nome del file senza estensione (es: 'user_hourly_summary')
        fallback: Testo di fallback se il file non esiste

    Returns:
        Template string con placeholder {variable_name}
    """
    if name in _cache:
        return _cache[name]

    path = _PROMPTS_DIR / f"{name}.txt"
    try:
        text = path.read_text(encoding="utf-8")
        _cache[name] = text
        return text
    except FileNotFoundError:
        if fallback:
            logger.warning(f"Prompt file not found: {path}, using fallback")
            _cache[name] = fallback
            return fallback
        raise FileNotFoundError(f"Prompt template not found: {path}")


def reload_prompts():
    """Svuota la cache dei prompt (utile per hot-reload)."""
    _cache.clear()
    logger.info("Prompt cache cleared")
