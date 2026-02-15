idf_component_register(
    SRCS "jarvis_network.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_wifi esp_event esp_netif nvs_flash esp_http_client cjson esp_timer
    PRIV_REQUIRES mbedtls
)
