# =============================================================================
# JARVIS AtomS3R - ESP-IDF Project
# =============================================================================
#
# Hardware: M5Stack AtomS3R (ESP32-S3 + PSRAM)
# Features:
#   - Wake Word Detection (ESP-SR WakeNet "jarvis")
#   - Streaming Audio with VAD
#   - Display TFT 128x128 (ST7789)
#   - WiFi connectivity to JARVIS Orchestrator
#
# Build: idf.py build
# Flash: idf.py -p /dev/ttyUSB0 flash monitor
# =============================================================================

cmake_minimum_required(VERSION 3.16)

# Imposta il target a ESP32-S3
set(IDF_TARGET "esp32s3")

# Include le componenti custom
set(EXTRA_COMPONENT_DIRS
    ${CMAKE_CURRENT_SOURCE_DIR}/components
)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(jarvis_atoms3r)

# =============================================================================
# Suppress warnings in WebRTC submodule components (peer, srtp, esp-libopus)
# These are third-party libraries with known type mismatches on Xtensa targets.
# Matches the approach used by OpenAI's reference embedded SDK.
# =============================================================================
cmake_language(DEFER DIRECTORY ${CMAKE_SOURCE_DIR} CALL _suppress_submodule_warnings)

function(_suppress_submodule_warnings)
    if(TARGET __idf_peer)
        target_compile_options(__idf_peer PRIVATE
            -Wno-error=restrict
            -Wno-error=stringop-truncation
            -Wno-error=discarded-qualifiers
            -Wno-error=implicit-function-declaration
            -Wno-error=int-conversion
        )
        message(STATUS "Suppressed warnings for peer component")
    endif()

    if(TARGET __idf_srtp)
        target_compile_options(__idf_srtp PRIVATE
            -Wno-error=incompatible-pointer-types
        )
        message(STATUS "Suppressed warnings for srtp component")
    endif()

    if(TARGET __idf_esp-libopus)
        target_compile_options(__idf_esp-libopus PRIVATE
            -Wno-error=maybe-uninitialized
            -Wno-error=stringop-overread
        )
        message(STATUS "Suppressed warnings for esp-libopus component")
    endif()
endfunction()

