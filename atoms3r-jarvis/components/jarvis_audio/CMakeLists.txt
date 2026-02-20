if(CONFIG_USE_LOCAL_WAKEWORD)
    # Local wake word mode: include TFLite model data and MWW dependencies
    idf_component_register(
        SRCS "jarvis_audio.cpp" "jarvis_wakeword_model.c"
        INCLUDE_DIRS "include" "."
        REQUIRES jarvis_codec esp_timer freertos esp_ringbuf esp_micro_speech_features
        PRIV_REQUIRES esp-tflite-micro
    )

    # TFLite Micro build flags
    target_compile_definitions(${COMPONENT_LIB} PUBLIC
        USE_LOCAL_WAKEWORD
        TF_LITE_STATIC_MEMORY
        TF_LITE_DISABLE_X86_NEON
        ESP_NN
    )
else()
    # Server-side wake word mode: no TFLite, no model data, no frontend
    idf_component_register(
        SRCS "jarvis_audio.cpp"
        INCLUDE_DIRS "include" "."
        REQUIRES jarvis_codec esp_timer freertos esp_ringbuf
    )
endif()
