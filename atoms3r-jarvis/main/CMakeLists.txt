idf_component_register(
    SRCS "main.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        jarvis_display
        jarvis_audio
        jarvis_network
        esp_wifi
        esp_event
        nvs_flash
        esp_netif
        esp_http_client
        cjson
        esp_timer
)
