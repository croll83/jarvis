"""HA Memory Service Prompt Templates Loader"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("HA_MEMORY_PROMPTS")

_PROMPTS_DIR = Path(__file__).parent
_cache: dict[str, str] = {}


def load_prompt(name: str, fallback: Optional[str] = None) -> str:
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
