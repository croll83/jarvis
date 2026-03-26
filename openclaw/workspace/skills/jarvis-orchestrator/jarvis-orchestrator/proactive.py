"""
JARVIS Proactive Notifications
- Anomaly detection dalle location
- Notifiche agli utenti
"""

import asyncio
import logging
from typing import Dict, Any, List
from datetime import datetime

import config
from database import get_location
from integrations import send_telegram
from location_memory import get_location_memory, MEMORY_SERVICE_URLS

logger = logging.getLogger("JARVIS_PROACTIVE")

# Soglie configurabili
ANOMALY_THRESHOLDS = {
    "door_open_minutes": config.PROACTIVE_DOOR_OPEN_MINUTES,
    "garage_open_night": True,
    "no_motion_hours": config.PROACTIVE_NO_MOTION_HOURS,
    "unusual_activity_night": True,
}

# Tracking anomalie gia' notificate (evita spam)
_notified_anomalies: Dict[str, float] = {}
_NOTIFICATION_COOLDOWN = config.INTERVALS["proactive_cooldown"]


async def check_location_anomalies(location_id: str) -> List[Dict[str, Any]]:
    """Controlla anomalie per una location."""
    memory = await get_location_memory(location_id, hot_minutes=60)
    if not memory:
        return []

    anomalies = []
    now = datetime.now()

    for event in memory.get("hot", []):
        entity = event.get("entity_id", "")
        state = event.get("new_state", "")
        timestamp = event.get("timestamp", 0)

        # Porta/finestra aperta da troppo tempo
        if any(x in entity for x in ["door", "porta", "finestra", "window"]):
            if state == "on":
                minutes_open = (now.timestamp() - timestamp) / 60
                if minutes_open > ANOMALY_THRESHOLDS["door_open_minutes"]:
                    anomaly_key = f"door_open:{entity}"
                    if _should_notify(anomaly_key):
                        anomalies.append({
                            "type": "door_open_long",
                            "entity": entity,
                            "duration_minutes": int(minutes_open),
                            "severity": "medium"
                        })

        # Attivita' notturna insolita
        if timestamp:
            event_hour = datetime.fromtimestamp(timestamp).hour
            if config.PROACTIVE_NIGHT_START <= event_hour <= config.PROACTIVE_NIGHT_END and ANOMALY_THRESHOLDS["unusual_activity_night"]:
                if any(x in entity for x in ["motion", "pir", "movimento"]):
                    anomaly_key = f"night_activity:{entity}"
                    if _should_notify(anomaly_key):
                        anomalies.append({
                            "type": "night_activity",
                            "entity": entity,
                            "hour": event_hour,
                            "severity": "high"
                        })

    return anomalies


def _should_notify(anomaly_key: str) -> bool:
    """Verifica se un'anomalia deve essere notificata (cooldown)."""
    import time
    now = time.time()
    last_notified = _notified_anomalies.get(anomaly_key, 0)
    if now - last_notified > _NOTIFICATION_COOLDOWN:
        _notified_anomalies[anomaly_key] = now
        return True
    return False


async def notify_anomaly(location_id: str, anomaly: Dict[str, Any]):
    """Notifica un'anomalia agli utenti."""
    loc = get_location(location_id)
    loc_name = loc.name if loc else location_id

    if anomaly["type"] == "door_open_long":
        message = f"[{loc_name}] {anomaly['entity']} aperto da {anomaly['duration_minutes']} minuti."
    elif anomaly["type"] == "night_activity":
        message = f"[{loc_name}] Movimento rilevato ({anomaly['entity']}) alle {anomaly['hour']}:00."
    else:
        message = f"[{loc_name}] Anomalia - {anomaly['type']}"

    await send_telegram(message)
    logger.info(f"Proactive notification: {anomaly['type']} @ {location_id}")


async def proactive_check_loop():
    """Loop principale per check proattivi."""
    # Attendi startup
    await asyncio.sleep(60)
    logger.info("Proactive check loop started")

    while True:
        for location_id in list(MEMORY_SERVICE_URLS.keys()):
            try:
                anomalies = await check_location_anomalies(location_id)
                for anomaly in anomalies:
                    await notify_anomaly(location_id, anomaly)
            except Exception as e:
                logger.error(f"Proactive check failed for {location_id}: {e}")

        await asyncio.sleep(config.INTERVALS["proactive_check"])
