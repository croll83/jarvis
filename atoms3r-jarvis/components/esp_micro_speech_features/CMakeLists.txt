idf_component_register(
    SRCS
        "src/window.c" "src/window_util.c"
        "src/fft.c" "src/fft_util.c"
        "src/filterbank.c" "src/filterbank_util.c"
        "src/noise_reduction.c" "src/noise_reduction_util.c"
        "src/pcan_gain_control.c" "src/pcan_gain_control_util.c"
        "src/log_scale.c" "src/log_scale_util.c"
        "src/log_lut.c"
        "src/kiss_fft.c" "src/kiss_fftr.c"
        "src/frontend.c" "src/frontend_util.c"
    INCLUDE_DIRS "include" "src"
)

# USE_ESP32 enables PSRAM-aware malloc in internal allocation functions
target_compile_definitions(${COMPONENT_LIB} PRIVATE USE_ESP32)

# Suppress warnings in third-party code
target_compile_options(${COMPONENT_LIB} PRIVATE
    -Wno-maybe-uninitialized
    -Wno-sign-compare
)
