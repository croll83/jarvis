idf_component_register(
    SRCS "jarvis_audio.cpp" "jarvis_wakeword_model.c"
    INCLUDE_DIRS "include" "."
    REQUIRES jarvis_codec esp_timer freertos esp_ringbuf esp_micro_speech_features
    PRIV_REQUIRES esp-tflite-micro
)

# TFLite Micro build flags
target_compile_definitions(${COMPONENT_LIB} PUBLIC
    TF_LITE_STATIC_MEMORY
    TF_LITE_DISABLE_X86_NEON
    ESP_NN
)
