idf_component_register(
    SRCS "jarvis_webrtc.c"
    INCLUDE_DIRS "include"
    REQUIRES peer esp-libopus jarvis_codec jarvis_audio
             esp_http_client esp_timer freertos esp_common cjson
)
