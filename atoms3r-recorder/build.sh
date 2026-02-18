#!/bin/bash
# =============================================================================
# AtomS3R Recorder - Build Helper
# =============================================================================
# Uso: ./build.sh [flash] [monitor]
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Source ESP-IDF 5.5
if [ -f ~/esp/esp-idf-v5.5/export.sh ]; then
    export PATH=$(echo "$PATH" | tr ':' '\n' | grep -v "esp-idf" | grep -v "espressif" | tr '\n' ':' | sed 's/:$//')
    unset IDF_PATH
    unset IDF_PYTHON_ENV_PATH
    unset IDF_TOOLS_PATH
    export IDF_PATH="$HOME/esp/esp-idf-v5.5"
    set +e
    source "$HOME/esp/esp-idf-v5.5/export.sh" >/dev/null 2>&1
    set -e
else
    echo "ESP-IDF v5.5 non trovato in ~/esp/esp-idf-v5.5/"
    exit 1
fi

export LC_CTYPE=en_US.UTF-8

# Applica sdkconfig.local
if [ -f sdkconfig.local ]; then
    echo "Applying sdkconfig.local..."
    if [ -f sdkconfig ]; then
        while IFS='=' read -r key value; do
            [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
            sed -i '' "/^${key}=/d" sdkconfig 2>/dev/null || true
            echo "${key}=${value}" >> sdkconfig
        done < sdkconfig.local
    else
        cp sdkconfig.defaults sdkconfig
        while IFS='=' read -r key value; do
            [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
            sed -i '' "/^${key}=/d" sdkconfig 2>/dev/null || true
            echo "${key}=${value}" >> sdkconfig
        done < sdkconfig.local
    fi
    echo "Local config applied"
else
    echo "sdkconfig.local non trovato - crea con WiFi + IP server"
    exit 1
fi

# Build
echo "Building recorder firmware..."
python ~/esp/esp-idf-v5.5/tools/idf.py build

# Flash
if [[ "$*" == *"flash"* ]]; then
    PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
    if [ -z "$PORT" ]; then
        PORT=$(ls /dev/cu.usbserial* 2>/dev/null | head -1)
    fi
    if [ -z "$PORT" ]; then
        echo "Nessuna porta USB trovata. Collega l'AtomS3R."
        exit 1
    fi
    echo "Flashing to $PORT..."
    python ~/esp/esp-idf-v5.5/tools/idf.py -p "$PORT" flash
fi

# Monitor
if [[ "$*" == *"monitor"* ]]; then
    PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
    if [ -z "$PORT" ]; then
        PORT=$(ls /dev/cu.usbserial* 2>/dev/null | head -1)
    fi
    echo "Starting monitor on $PORT... (Ctrl+] to exit)"
    python ~/esp/esp-idf-v5.5/tools/idf.py -p "$PORT" monitor
fi
