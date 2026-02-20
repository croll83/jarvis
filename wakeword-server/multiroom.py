"""Multi-room cooldown logic.

After wake detection on one device, all other devices in the same
server are muted for MULTIROOM_COOLDOWN_S seconds to prevent
multiple devices triggering simultaneously.
"""

import logging
import time
from typing import Dict

from config import MULTIROOM_COOLDOWN_S

logger = logging.getLogger("wakeword-server")


class MultiroomCooldown:
    """Tracks cooldown state across all connected devices."""

    def __init__(self, cooldown_s: int = MULTIROOM_COOLDOWN_S):
        self.cooldown_s = cooldown_s
        self._last_wake_time: float = 0.0
        self._last_wake_device: str = ""

    def on_wake(self, device_id: str):
        """Called when a wake word is detected on a device."""
        self._last_wake_time = time.monotonic()
        self._last_wake_device = device_id
        logger.info(f"Multi-room cooldown started: {self.cooldown_s}s "
                     f"(triggered by {device_id})")

    def is_cooled_down(self, device_id: str) -> bool:
        """Returns True if this device should IGNORE wake detections (still in cooldown).

        The device that triggered the cooldown is never blocked.
        """
        if device_id == self._last_wake_device:
            return False
        return time.monotonic() < (self._last_wake_time + self.cooldown_s)
