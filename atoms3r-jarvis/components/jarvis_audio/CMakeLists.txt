idf_component_register(
    SRCS "jarvis_audio.c"
    INCLUDE_DIRS "include"
    REQUIRES jarvis_codec esp_timer freertos esp_ringbuf
    PRIV_REQUIRES esp-sr
)
