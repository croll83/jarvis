#!/bin/bash
# =============================================================================
# JARVIS AtomS3R - Build Helper
# =============================================================================
# Applica sdkconfig.local (credenziali) sopra sdkconfig.defaults prima del build.
# Uso: ./build.sh [flash] [monitor]
#
# Esempi:
#   ./build.sh                  # Solo build
#   ./build.sh flash            # Build + flash
#   ./build.sh flash monitor    # Build + flash + monitor seriale
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Source ESP-IDF 5.5 environment
# Must fully clean any previously loaded IDF (e.g. 6.1 from shell profile)
if [ -f ~/esp/esp-idf-v5.5/export.sh ]; then
    # Remove any existing IDF/espressif paths from PATH to avoid conflicts
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

# Applica sdkconfig.local se esiste
if [ -f sdkconfig.local ]; then
    echo "🔑 Applying sdkconfig.local (local credentials)..."
    # Leggi ogni riga CONFIG_* da sdkconfig.local e sovrascrivila in sdkconfig
    # (se sdkconfig non esiste ancora, verrà creato dal build)
    if [ -f sdkconfig ]; then
        while IFS='=' read -r key value; do
            # Salta commenti e righe vuote
            [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
            # Rimuovi la vecchia riga e aggiungi la nuova
            sed -i '' "/^${key}=/d" sdkconfig 2>/dev/null || true
            echo "${key}=${value}" >> sdkconfig
        done < sdkconfig.local
    else
        # sdkconfig non esiste: primo build. Copia defaults + local
        cp sdkconfig.defaults sdkconfig
        while IFS='=' read -r key value; do
            [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
            sed -i '' "/^${key}=/d" sdkconfig 2>/dev/null || true
            echo "${key}=${value}" >> sdkconfig
        done < sdkconfig.local
    fi
    echo "✅ Local config applied"
else
    echo "⚠️  sdkconfig.local non trovato - usando solo sdkconfig.defaults (placeholder)"
    echo "   Crea sdkconfig.local con le tue credenziali (vedi sdkconfig.local.example)"
fi

# Build
echo "🔨 Building firmware..."
python ~/esp/esp-idf-v5.5/tools/idf.py build

# Flash e/o Monitor se richiesto
if [[ "$*" == *"flash"* ]]; then
    PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
    if [ -z "$PORT" ]; then
        PORT=$(ls /dev/cu.usbserial* 2>/dev/null | head -1)
    fi
    if [ -z "$PORT" ]; then
        echo "❌ Nessuna porta USB trovata. Collega l'AtomS3R."
        exit 1
    fi
    echo "📡 Flashing to $PORT..."
    python ~/esp/esp-idf-v5.5/tools/idf.py -p "$PORT" flash
fi

if [[ "$*" == *"monitor"* ]]; then
    PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
    if [ -z "$PORT" ]; then
        PORT=$(ls /dev/cu.usbserial* 2>/dev/null | head -1)
    fi
    echo "📺 Starting monitor on $PORT... (Ctrl+] to exit)"
    python ~/esp/esp-idf-v5.5/tools/idf.py -p "$PORT" monitor
fi
