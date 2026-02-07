idf_component_register(
    SRCS "jarvis_audio.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_driver_i2s esp_timer
    PRIV_REQUIRES esp-sr
)
