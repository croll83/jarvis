idf_component_register(
    SRCS "jarvis_speaker.c"
    INCLUDE_DIRS "include"
    REQUIRES jarvis_codec esp_common freertos
)
