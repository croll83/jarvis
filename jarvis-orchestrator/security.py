import time
import logging
from typing import Optional, List

import config
from database import get_global_preference
from database import (
    get_system_state, set_system_state, log_event, get_user_by_name,
    get_all_locations, get_cameras_by_category, get_global_preference
)
from integrations import call_hass_service, speak, send_telegram

logger = logging.getLogger("JARVIS_SECURITY")


def is_family_member(name: str) -> bool:
    """
    Verifica se un nome corrisponde a un utente registrato nel DB.
    Usato per riconoscimento facciale da Frigate/DoubleTake.
    """
    if not name:
        return False
    user = get_user_by_name(name)
    return user is not None


def _get_security_location() -> Optional[str]:
    """Trova la location con has_security=True."""
    locations = get_all_locations(enabled_only=True)
    for loc in locations:
        if loc.has_security:
            return loc.id
    return None


def _get_internal_cameras(location_id: str) -> List[str]:
    """Recupera le camere interne dal DB."""
    return get_cameras_by_category(location_id, "internal")


def _get_perimetral_cameras(location_id: str) -> List[str]:
    """Recupera le camere perimetrali dal DB."""
    return get_cameras_by_category(location_id, "perimetral")


def _get_security_suppression_time() -> int:
    """Recupera il tempo di soppressione allarmi dal DB."""
    return int(get_global_preference("security_suppression_time", "600"))


class SecurityManager:
    """Gestisce la sicurezza della casa: privacy mode, riconoscimento facciale, allarmi."""

    def __init__(self):
        # Carica l'ultimo stato noto dal DB
        self.privacy_mode = get_system_state("privacy_mode", "False") == "True"
        self.last_family_seen = float(get_system_state("last_family_seen", "0"))

    async def set_privacy_mode(self, activate: bool, location_id: str = None) -> str:
        """Attiva o disattiva la modalità Privacy agendo sulle telecamere."""
        self.privacy_mode = activate

        # Determina location
        loc_id = location_id or _get_security_location()
        if not loc_id:
            logger.warning("No security location configured")
            return "Nessuna location con sicurezza configurata"

        # Salva subito su DB
        set_system_state("privacy_mode", str(activate))
        log_event("SECURITY", f"Privacy Mode {'ATTIVATA' if activate else 'DISATTIVATA'}")

        action = "turn_off" if activate else "turn_on"

        # 1. Gestione Telecamere Interne (Standby fisico)
        internal_cameras = _get_internal_cameras(loc_id)
        for cam in internal_cameras:
            await call_hass_service(loc_id, "camera", action, {"entity_id": cam})

        # 2. Gestione Notifiche EZVIZ (Disabilita motion alert nativo)
        ezviz_service = "disable_motion_detection" if activate else "enable_motion_detection"
        perimetral_cameras = _get_perimetral_cameras(loc_id)
        for cam in perimetral_cameras:
            await call_hass_service(loc_id, "ezviz", ezviz_service, {"entity_id": cam})

        status_text = "Privacy ATTIVA. Telecamere interne spente." if activate else "Sistema ARMATO. Protezione totale."
        logger.info(status_text)
        return status_text

    async def handle_camera_event(self, event_data: dict):
        """
        Logica di elaborazione eventi da Frigate + DoubleTake.
        event_data: { 'label': 'Marco', 'zone': 'piscina', 'camera': 'NW' }
        """
        label = event_data.get("label", "unknown")
        zone = event_data.get("zone", "perimetro")
        camera = event_data.get("camera", "unknown")
        now = time.time()

        # --- CASO 1: È UN MEMBRO DELLA FAMIGLIA ---
        if is_family_member(label):
            self.last_family_seen = now
            set_system_state("last_family_seen", str(now))
            
            logger.info(f"Riconosciuto membro Family: {label} in zona {zone}.")
            log_event("FAMILY", f"{label} rilevato in {zone} (camera: {camera})")

            # Se siamo tornati a casa e la privacy era OFF, la attiviamo
            if not self.privacy_mode:
                msg = await self.set_privacy_mode(True)
                await send_telegram(f"🛡️ Privacy attivata automaticamente: Benvenuto {label}!")
                await speak(
                    f"Bentornato {label}. Ho attivato la modalità privacy.", 
                    get_global_preference("security_announcement_speaker", config.SECURITY_ANNOUNCEMENT_SPEAKER)
                )
            return

        # --- CASO 2: ANIMALI (Solo Telegram, no voce) ---
        if label in config.SECURITY_ANIMAL_LABELS:
            await send_telegram(f"🐾 Movimento animale: {label} visto in {zone}.")
            log_event("PET", f"{label} rilevato in {zone}")
            logger.info(f"Pet detected in {zone}, telegram notification sent.")
            return

        # --- CASO 3: UMANO GENERICO (Potenziale Intruso) ---
        if label == "person":
            # Se privacy è OFF, siamo in modalità "sorveglianza attiva"
            if not self.privacy_mode:
                logger.debug(f"Person detected in {zone}, but privacy mode is OFF (surveillance active)")
                return
            
            # Calcoliamo quanto tempo è passato dall'ultimo avvistamento Family
            time_since_family = now - self.last_family_seen

            # SOPPRESSIONE ALLARME (La regola dei 10 minuti)
            suppression_time = _get_security_suppression_time()
            if time_since_family < suppression_time:
                logger.info(
                    f"Rilevato 'person' in {zone}, ma ignorato: "
                    f"familiare visto {int(time_since_family)}s fa."
                )
                return

            # SE ARRIVIAMO QUI: È un vero allarme
            alert_msg = (
                f"ATTENZIONE: Rilevato intruso nella zona {zone}. "
                f"Nessun familiare identificato nei paraggi."
            )

            logger.warning(f"🚨 ALLARME SICUREZZA: {alert_msg}")
            log_event("SECURITY_ALERT", f"Intruso rilevato in {zone} (camera: {camera})")

            # Notifica urgente su Telegram
            await send_telegram(
                f"🚨 *ALLERTA INTRUSO* 🚨\n"
                f"Zona: {zone}\n"
                f"Camera: {camera}\n"
                f"Nessun familiare visto da {int(time_since_family/60)} min."
            )

            # Avviso vocale deterrente sulla Soundbar
            await speak(
                f"Attenzione. Identificata persona sconosciuta in zona {zone}.",
                get_global_preference("security_announcement_speaker", config.SECURITY_ANNOUNCEMENT_SPEAKER)
            )


# ===========================================================================
# CHANNEL-BASED ACTION SECURITY
# ===========================================================================

def should_require_approval(domain: str, action: str, source: str) -> bool:
    """
    Determina se un'azione richiede approvazione manuale.
    Based on source channel trust level.
    """
    # Voice commands from voice devices are trusted (speaker ID verified)
    if source in config.VOICE_SOURCES:
        return False
    
    # Telegram via OpenClaw — check critical domains
    if source == "Telegram" or source == "openclaw":
        if domain in config.CRITICAL_DOMAINS:
            return True
        # Auto-approve safe actions
        if domain in config.AUTO_APPROVE_ACTIONS:
            allowed = config.AUTO_APPROVE_ACTIONS[domain]
            if action in allowed or "*" in allowed:
                return False
        # Default: approve for non-critical
        return False
    
    # Unknown/external sources — always require approval
    return True


def get_approval_priority(domain: str, action: str) -> str:
    """
    Determina la priorità dell'approvazione (HIGH/NORMAL).
    """
    if domain in config.CRITICAL_DOMAINS:
        return "HIGH"
    
    if domain in config.ALWAYS_CONFIRM_ACTIONS:
        return "HIGH"
    
    return "NORMAL"
