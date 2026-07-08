"""Mappa stanza → player musicale e resolver, condivisi tra il fast-path del
router (main.py) e i tool per l'AI agent (tools_api.py).

Soundbar salotto = player MASS nativo; le altre stanze sono Echo esposti in HA
dal provider alexa di Music Assistant; "ovunque" è il gruppo che suona in tutta
la casa.
"""

MUSIC_PLAYERS = {
    "wagmi": {
        "salotto": "media_player.soundbar_salotto_5",
        "soggiorno": "media_player.soundbar_salotto_5",
        "zona giorno": "media_player.soundbar_salotto_5",
        "ufficio": "media_player.echo_ufficio_2",
        "cameretta": "media_player.echo_cameretta_2",
        "camera": "media_player.echo_camera_2",
        "depandance": "media_player.echo_depandance_2",
        "ovunque": "media_player.ovunque_2",
        "tutta la casa": "media_player.ovunque_2",
        "casa": "media_player.ovunque_2",
        None: "media_player.soundbar_salotto_5",
    }
}


def resolve_music_player(location: str, room_text: str | None) -> tuple[str | None, str]:
    """Room text → (player entity_id, label). Longest-key match wins so
    'cameretta' non viene catturata da 'camera'."""
    players = MUSIC_PLAYERS.get(location, {})
    if not players:
        return None, ""
    rt = (room_text or "").strip().lower()
    if rt:
        for key in sorted((k for k in players if k), key=len, reverse=True):
            if key in rt or rt in key:
                return players[key], key
    return players.get(None), "salotto"
