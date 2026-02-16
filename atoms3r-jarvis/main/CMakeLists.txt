idf_component_register(
    SRCS "main.cpp" "compat_stub.c"
    INCLUDE_DIRS "."
    REQUIRES
        jarvis_display
        jarvis_codec
        jarvis_audio
        jarvis_network
        jarvis_speaker
        jarvis_ws_audio
        esp_wifi
        esp_event
        nvs_flash
        esp_netif
        esp_http_client
        cjson
        esp_timer
)

# Force linker to pull in __getreent from compat_stub.c
# Required because esp-sr pre-compiled libs reference this symbol
# which was removed (replaced by macro) in ESP-IDF v6.1
target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,-u,__getreent")
