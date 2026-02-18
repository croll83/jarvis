idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES jarvis_codec esp_wifi esp_event esp_netif nvs_flash esp_timer freertos lwip
)
