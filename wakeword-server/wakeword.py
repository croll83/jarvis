"""openWakeWord wrapper — one inference engine per connected device."""

import logging
import time
from typing import Optional

import numpy as np
from openwakeword.model import Model

from config import WAKEWORD_MODEL, WAKEWORD_THRESHOLD, OPUS_SAMPLE_RATE

logger = logging.getLogger("wakeword-server")


class DeviceWakeWordEngine:
    """Per-device openWakeWord inference engine."""

    def __init__(self, device_id: str, model_name: str = WAKEWORD_MODEL,
                 threshold: float = WAKEWORD_THRESHOLD):
        self.device_id = device_id
        self.model_name = model_name
        self.threshold = threshold
        self._model: Optional[Model] = None
        self._muted_until: float = 0.0  # timestamp until which detection is suppressed

    def init(self):
        """Load model (call once per device connection)."""
        self._model = Model(wakeword_models=[self.model_name])
        logger.info(f"[{self.device_id}] openWakeWord model '{self.model_name}' loaded")

    def set_threshold(self, threshold: float):
        """Update detection threshold (0.1–1.0)."""
        self.threshold = max(0.1, min(1.0, threshold))
        logger.info(f"[{self.device_id}] threshold set to {self.threshold}")

    def mute(self, duration_s: float):
        """Suppress detection for the given duration (multi-room cooldown, busy state)."""
        self._muted_until = time.monotonic() + duration_s

    @property
    def is_muted(self) -> bool:
        return time.monotonic() < self._muted_until

    def process_audio(self, pcm_int16: np.ndarray) -> bool:
        """Feed PCM int16 samples; returns True if wake word detected.

        Args:
            pcm_int16: numpy array of int16 samples (16 kHz mono).
        """
        if self._model is None or self.is_muted:
            return False

        # openWakeWord expects int16 numpy array
        predictions = self._model.predict(pcm_int16)

        for mdl_name, score in predictions.items():
            if score >= self.threshold:
                logger.info(f"[{self.device_id}] WAKE detected (model={mdl_name}, "
                            f"score={score:.3f}, threshold={self.threshold})")
                return True
        return False

    def reset(self):
        """Reset model state (e.g. after a detection)."""
        if self._model:
            self._model.reset()

    def close(self):
        self._model = None
