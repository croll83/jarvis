idf_component_register(
    SRCS "jarvis_ws_audio.c"
    INCLUDE_DIRS "include"
    REQUIRES esp-libopus jarvis_codec jarvis_audio
             esp_websocket_client esp_timer freertos esp_common cjson esp_event
)
