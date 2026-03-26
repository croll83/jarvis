#!/usr/bin/env bash
# wireproxy-launcher.sh
# Iterates through Mullvad WireGuard configs, starts wireproxy
# with the first one that establishes a working SOCKS5 proxy.
# On success, stays alive. If wireproxy dies, exits so systemd restarts us.

set -euo pipefail

CONF_DIR="$(dirname "$0")"
WIREPROXY_CONF="/tmp/wireproxy-active.conf"
SOCKS_PORT="${WIREPROXY_SOCKS_PORT:-1080}"
TEST_TIMEOUT=10
MAX_ATTEMPTS="${WIREPROXY_MAX_ATTEMPTS:-0}"  # 0 = try all

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# Build wireproxy config from a WireGuard .conf
build_wireproxy_conf() {
    local wg_conf="$1"

    local private_key address dns peer_pubkey endpoint allowed_ips
    private_key=$(grep -oP 'PrivateKey\s*=\s*\K.+' "$wg_conf" | tr -d ' ')
    address=$(grep -oP 'Address\s*=\s*\K[^,]+' "$wg_conf" | head -1 | tr -d ' ')
    dns=$(grep -oP 'DNS\s*=\s*\K.+' "$wg_conf" | tr -d ' ')
    peer_pubkey=$(grep -oP 'PublicKey\s*=\s*\K.+' "$wg_conf" | tr -d ' ')
    endpoint=$(grep -oP 'Endpoint\s*=\s*\K.+' "$wg_conf" | tr -d ' ')
    allowed_ips=$(grep -oP 'AllowedIPs\s*=\s*\K.+' "$wg_conf" | tr -d ' ')

    cat > "$WIREPROXY_CONF" <<WPCEOF
[Interface]
PrivateKey = ${private_key}
Address = ${address}
DNS = ${dns}

[Peer]
PublicKey = ${peer_pubkey}
Endpoint = ${endpoint}
AllowedIPs = ${allowed_ips}
PersistentKeepalive = 25

[Socks5]
BindAddress = 127.0.0.1:${SOCKS_PORT}
WPCEOF
}

# Test if the SOCKS5 proxy works
test_proxy() {
    sleep 3
    for attempt in 1 2 3; do
        if curl -s --max-time "$TEST_TIMEOUT" \
             --proxy "socks5h://127.0.0.1:${SOCKS_PORT}" \
             "https://api.ipify.org" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

# Cleanup on exit
cleanup() {
    [[ -n "${WIREPROXY_PID:-}" ]] && kill "$WIREPROXY_PID" 2>/dev/null || true
}
trap cleanup EXIT

# Shuffle configs for load distribution
mapfile -t configs < <(find "$CONF_DIR" -name '*.conf' -type f | shuf)

if [[ ${#configs[@]} -eq 0 ]]; then
    log "ERROR: No .conf files found in $CONF_DIR"
    exit 1
fi

log "Found ${#configs[@]} Mullvad configs. Testing..."

tried=0
for conf in "${configs[@]}"; do
    name=$(basename "$conf" .conf)
    tried=$((tried + 1))

    if [[ "$MAX_ATTEMPTS" -gt 0 && "$tried" -gt "$MAX_ATTEMPTS" ]]; then
        log "ERROR: Reached max attempts ($MAX_ATTEMPTS), giving up."
        exit 1
    fi

    log "[$tried/${#configs[@]}] Trying $name..."

    build_wireproxy_conf "$conf"

    # Start wireproxy in background
    wireproxy -c "$WIREPROXY_CONF" &
    WIREPROXY_PID=$!

    # Check if process is still alive
    sleep 1
    if ! kill -0 "$WIREPROXY_PID" 2>/dev/null; then
        log "  ✗ wireproxy crashed on startup"
        wait "$WIREPROXY_PID" 2>/dev/null || true
        continue
    fi

    # Test connectivity
    if test_proxy; then
        EXIT_IP=$(curl -s --max-time 5 --proxy "socks5h://127.0.0.1:${SOCKS_PORT}" "https://api.ipify.org" 2>/dev/null || echo "unknown")
        log "  ✓ SUCCESS! Server: $name | Exit IP: $EXIT_IP | SOCKS5: 127.0.0.1:${SOCKS_PORT}"

        # Save active config name for reference
        echo "$name" > /tmp/wireproxy-active-server.txt

        # Stay alive — wait on wireproxy process
        wait "$WIREPROXY_PID"

        # If wireproxy dies, exit with error (systemd will restart us)
        log "wireproxy exited. Daemon will be restarted by systemd."
        exit 1
    else
        log "  ✗ No connectivity through $name"
        kill "$WIREPROXY_PID" 2>/dev/null || true
        wait "$WIREPROXY_PID" 2>/dev/null || true
        unset WIREPROXY_PID
    fi
done

log "ERROR: All ${#configs[@]} configs failed."
exit 1
