idf_component_register(
    SRCS "jarvis_codec.c" "es8311.c"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "priv_include"
    REQUIRES esp_driver_i2s esp_driver_i2c esp_common freertos
)
