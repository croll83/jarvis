idf_component_register(
    SRCS "jarvis_display.c"
    INCLUDE_DIRS "include"
    REQUIRES driver esp_lcd esp_timer esp_driver_i2c
)
