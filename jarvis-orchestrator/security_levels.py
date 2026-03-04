"""
JARVIS Security Levels (L1-L4)

Channel-based security model for home automation actions.
Prevents prompt injection attacks via untrusted channels (email, etc.)
while keeping voice commands fully trusted (certified by Resemblyzer).

All mappings are configurable via database (admin dashboard).
"""

import logging
from enum import IntEnum
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("JARVIS_SECURITY")


class SecurityLevel(IntEnum):
    """Security levels for home automation actions."""

    L1_SAFE = 1  # lights, sensors, volume, input_boolean, reads
    L2_SENSITIVE = 2  # covers, climate, fan
    L3_PROTECTED = 3  # locks, alarm, cameras
    L4_BLOCKED = 4  # always blocked from non-trusted sources


# ═══════════════════════════════════════════════════════════════════════════════
# DEFAULT DOMAIN → SECURITY LEVEL MAPPING
# ═══════════════════════════════════════════════════════════════════════════════
# Overrideable via DB table `security_domain_levels`

DEFAULT_DOMAIN_LEVELS: Dict[str, SecurityLevel] = {
    # L1 — Safe: low-risk, reversible actions
    "light": SecurityLevel.L1_SAFE,
    "switch": SecurityLevel.L1_SAFE,
    "sensor": SecurityLevel.L1_SAFE,
    "binary_sensor": SecurityLevel.L1_SAFE,
    "media_player": SecurityLevel.L1_SAFE,
    "input_boolean": SecurityLevel.L1_SAFE,
    "input_number": SecurityLevel.L1_SAFE,
    "input_select": SecurityLevel.L1_SAFE,
    "input_text": SecurityLevel.L1_SAFE,
    "scene": SecurityLevel.L1_SAFE,
    "script": SecurityLevel.L1_SAFE,
    "automation": SecurityLevel.L1_SAFE,
    "button": SecurityLevel.L1_SAFE,
    "number": SecurityLevel.L1_SAFE,
    "select": SecurityLevel.L1_SAFE,
    # L2 — Sensitive: physical actuators, comfort systems
    "cover": SecurityLevel.L2_SENSITIVE,
    "climate": SecurityLevel.L2_SENSITIVE,
    "fan": SecurityLevel.L2_SENSITIVE,
    "humidifier": SecurityLevel.L2_SENSITIVE,
    "water_heater": SecurityLevel.L2_SENSITIVE,
    "valve": SecurityLevel.L2_SENSITIVE,
    # L3 — Protected: security-critical
    "lock": SecurityLevel.L3_PROTECTED,
    "alarm_control_panel": SecurityLevel.L3_PROTECTED,
    "camera": SecurityLevel.L3_PROTECTED,
    "siren": SecurityLevel.L3_PROTECTED,
    # L4 — Blocked: never auto-allowed
    # (no default domains — L4 is for channel-based blocking)
}


# ═══════════════════════════════════════════════════════════════════════════════
# CHANNEL → MAX SECURITY LEVEL MAPPING
# ═══════════════════════════════════════════════════════════════════════════════
# Each channel has a maximum security level it can execute.
# Voice (Resemblyzer-certified) is fully trusted.
# OpenClaw Telegram is L2 (L3 requires JARVIS approval bot).
# Email/unknown sources are always blocked.

DEFAULT_CHANNEL_PERMISSIONS: Dict[str, SecurityLevel] = {
    "voice": SecurityLevel.L3_PROTECTED,  # Resemblyzer verified — fully trusted
    "telegram": SecurityLevel.L3_PROTECTED,  # Direct JARVIS bot — trusted (owner only)
    "openclaw_telegram": SecurityLevel.L2_SENSITIVE,  # Telegram via OpenClaw
    "openclaw_whatsapp": SecurityLevel.L2_SENSITIVE,  # Whatsapp via OpenClaw
    "openclaw_app": SecurityLevel.L3_PROTECTED,  # Mac/desktop app — configurable as trusted
    "api_tools": SecurityLevel.L2_SENSITIVE,  # Direct API tool calls from OpenClaw
    "email": SecurityLevel.L4_BLOCKED,  # Always blocked for home control
    "unknown": SecurityLevel.L4_BLOCKED,  # Unknown sources — blocked
}


def get_domain_level(
    domain: str, db_overrides: Optional[Dict[str, int]] = None
) -> SecurityLevel:
    """
    Get the security level for a Home Assistant domain.

    Args:
        domain: HA domain (e.g., "light", "lock", "cover")
        db_overrides: Optional dict of domain→level from database

    Returns:
        SecurityLevel for the domain
    """
    # Check DB overrides first
    if db_overrides and domain in db_overrides:
        try:
            return SecurityLevel(db_overrides[domain])
        except ValueError:
            logger.warning(
                f"Invalid security level {db_overrides[domain]} for domain {domain}"
            )

    # Fall back to defaults
    return DEFAULT_DOMAIN_LEVELS.get(domain, SecurityLevel.L2_SENSITIVE)


def get_channel_permission(
    channel: str, db_overrides: Optional[Dict[str, int]] = None
) -> SecurityLevel:
    """
    Get the maximum security level a channel can execute.

    Args:
        channel: Source channel (e.g., "voice", "openclaw_telegram", "email")
        db_overrides: Optional dict of channel→level from database

    Returns:
        Maximum SecurityLevel the channel is allowed to execute
    """
    # Check DB overrides first
    if db_overrides and channel in db_overrides:
        try:
            return SecurityLevel(db_overrides[channel])
        except ValueError:
            logger.warning(
                f"Invalid channel permission {db_overrides[channel]} for channel {channel}"
            )

    # Fall back to defaults
    return DEFAULT_CHANNEL_PERMISSIONS.get(channel, SecurityLevel.L4_BLOCKED)


def check_security(
    domain: str,
    service: str,
    source_channel: str,
    entity_id: Optional[str] = None,
    db_domain_overrides: Optional[Dict[str, int]] = None,
    db_channel_overrides: Optional[Dict[str, int]] = None,
) -> Tuple[bool, str, SecurityLevel, SecurityLevel]:
    """
    Check if an action is allowed based on security levels.

    Args:
        domain: HA domain (e.g., "light", "lock")
        service: HA service (e.g., "turn_on", "lock", "unlock")
        source_channel: Channel the request came from
        entity_id: Optional specific entity for logging
        db_domain_overrides: Optional domain→level overrides from DB
        db_channel_overrides: Optional channel→level overrides from DB

    Returns:
        Tuple of (allowed, reason, domain_level, channel_max_level)
    """
    domain_level = get_domain_level(domain, db_domain_overrides)
    channel_max = get_channel_permission(source_channel, db_channel_overrides)

    entity_str = f" ({entity_id})" if entity_id else ""

    # L4 sources are always blocked
    if channel_max == SecurityLevel.L4_BLOCKED:
        reason = (
            f"BLOCKED: Channel '{source_channel}' is not authorized for home control. "
            f"Action {domain}.{service}{entity_str} denied."
        )
        logger.warning(f"L4 BLOCK: {reason}")
        return False, reason, domain_level, channel_max

    # Check if the action's security level exceeds the channel's permission
    if domain_level > channel_max:
        if domain_level == SecurityLevel.L3_PROTECTED:
            reason = (
                f"APPROVAL_REQUIRED: Action {domain}.{service}{entity_str} requires L3 approval. "
                f"Channel '{source_channel}' max level is L{channel_max.value}. "
                f"Confirmation will be sent via JARVIS approval bot."
            )
            logger.info(f"L3 approval needed: {reason}")
            return False, reason, domain_level, channel_max
        else:
            reason = (
                f"DENIED: Action {domain}.{service}{entity_str} requires L{domain_level.value} "
                f"but channel '{source_channel}' only allows up to L{channel_max.value}."
            )
            logger.warning(f"Security denied: {reason}")
            return False, reason, domain_level, channel_max

    # Action is allowed
    reason = (
        f"ALLOWED: {domain}.{service}{entity_str} (L{domain_level.value}) "
        f"via {source_channel} (max L{channel_max.value})"
    )
    logger.debug(reason)
    return True, reason, domain_level, channel_max


def needs_approval(
    domain: str,
    source_channel: str,
    db_domain_overrides: Optional[Dict[str, int]] = None,
    db_channel_overrides: Optional[Dict[str, int]] = None,
) -> bool:
    """
    Quick check: does this action need L3 approval?

    Returns True if the domain requires a higher security level than the channel provides.
    """
    domain_level = get_domain_level(domain, db_domain_overrides)
    channel_max = get_channel_permission(source_channel, db_channel_overrides)
    return domain_level > channel_max and domain_level == SecurityLevel.L3_PROTECTED


def get_security_summary() -> Dict[str, Any]:
    """
    Get a summary of the security configuration for the admin dashboard.
    """
    return {
        "levels": {
            "L1_SAFE": {
                "description": "Luci, sensori, volume, input, scene",
                "domains": [
                    d
                    for d, l in DEFAULT_DOMAIN_LEVELS.items()
                    if l == SecurityLevel.L1_SAFE
                ],
            },
            "L2_SENSITIVE": {
                "description": "Tapparelle, clima, ventilatori",
                "domains": [
                    d
                    for d, l in DEFAULT_DOMAIN_LEVELS.items()
                    if l == SecurityLevel.L2_SENSITIVE
                ],
            },
            "L3_PROTECTED": {
                "description": "Serrature, allarme, telecamere",
                "domains": [
                    d
                    for d, l in DEFAULT_DOMAIN_LEVELS.items()
                    if l == SecurityLevel.L3_PROTECTED
                ],
            },
            "L4_BLOCKED": {
                "description": "Sempre bloccato da fonti non trusted",
                "domains": [],
            },
        },
        "channels": {
            name: {
                "max_level": f"L{level.value}",
                "description": _channel_description(name),
            }
            for name, level in DEFAULT_CHANNEL_PERMISSIONS.items()
        },
    }


def _channel_description(channel: str) -> str:
    """Human-readable channel descriptions."""
    descriptions = {
        "voice": "Comandi vocali (certificati da Resemblyzer) — completamente trusted",
        "openclaw_telegram": "Telegram via OpenClaw — trusted per L1-L2",
        "openclaw_app": "App Mac/desktop OpenClaw — configurabile come trusted",
        "api_tools": "Chiamate API dirette da OpenClaw — L1-L2",
        "email": "Email — sempre bloccato per controllo domotica",
        "unknown": "Fonte sconosciuta — sempre bloccato",
    }
    return descriptions.get(channel, "Canale non riconosciuto")
