idf_component_register(
    SRCS "jarvis_speaker.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_driver_i2s esp_common freertos
)
