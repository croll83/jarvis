# SOCKS5 Proxy Setup for Polymarket

## Why do we need a proxy?

Polymarket's CLOB (Central Limit Order Book) API is **geoblocked** for certain jurisdictions. If the server's IP is in a blocked region, all CLOB operations (trading, balance checks, order placement) will fail with connection or geoblock errors.

**What IS geoblocked:**
- CLOB API (`clob.polymarket.com`) — trading, orders, balances, authentication
- Gamma API (`gamma-api.polymarket.com`) — market listings, search, events

**What is NOT geoblocked:**
- On-chain RPC (`polygon.drpc.org`) — approvals, redemptions, wallet operations
- The polymarket-cli automatically excludes RPC traffic from the proxy via `NO_PROXY`

## Architecture

```
polymarket-cli
    │
    ├── CLOB/Gamma requests ──→ SOCKS5 proxy (127.0.0.1:1080)
    │                              │
    │                              └── wireproxy (userspace WireGuard)
    │                                     │
    │                                     └── Mullvad VPN server (Spain)
    │                                            │
    │                                            └── polymarket.com
    │
    └── On-chain RPC requests ──→ direct (polygon.drpc.org, not proxied)
```

**wireproxy** is a userspace WireGuard client that exposes a SOCKS5 proxy. No kernel module, no root for the tunnel itself — just a single binary. The polymarket-cli has built-in SOCKS5 support (our fork) and reads the proxy URL from its config file.

## Setup Steps

### 1. Install wireproxy

Download the latest release from GitHub:

```bash
# Check latest version at: https://github.com/pufferffish/wireproxy/releases
WIREPROXY_VERSION="1.0.9"
wget "https://github.com/pufferffish/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_amd64.tar.gz"
tar xzf wireproxy_linux_amd64.tar.gz
sudo mv wireproxy /usr/local/bin/
sudo chmod +x /usr/local/bin/wireproxy
rm wireproxy_linux_amd64.tar.gz

wireproxy --version
# wireproxy, version 1.0.9
```

### 2. Get Mullvad WireGuard configs

You need a Mullvad VPN account (https://mullvad.net). Generate WireGuard config files:

1. Go to https://mullvad.net/en/account/wireguard-config
2. Select **Country**: Spain (or any country not geoblocked by Polymarket)
3. Select **Port**: 51820
4. Download configs for **multiple servers** (for failover)

Place the `.conf` files in a directory:

```bash
mkdir -p ~/mullvad-wg-configs
# Copy your downloaded .conf files here:
# es-bcn-wg-001.conf, es-bcn-wg-002.conf, es-mad-wg-101.conf, etc.
```

**Which country to use?** Spain (ES) is confirmed working. You can use any country where Polymarket is accessible. Avoid sanctioned jurisdictions. Download configs from multiple cities for redundancy.

See `example-wg.conf` for the expected format.

### 3. Install the launcher script

```bash
cp wireproxy-launcher.sh ~/mullvad-wg-configs/
chmod +x ~/mullvad-wg-configs/wireproxy-launcher.sh
```

The launcher script:
- Shuffles all `.conf` files in the directory (load distribution)
- Tries each one until a working SOCKS5 tunnel is established
- Tests connectivity by fetching the exit IP through the proxy
- Stays alive on the working connection
- If the tunnel drops, exits so systemd can restart and pick a new server

### 4. Install and enable the systemd service

```bash
# Edit the service file first:
# - Change User= to your username
# - Change ExecStart= path to where you put wireproxy-launcher.sh
sudo cp wireproxy-mullvad.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wireproxy-mullvad
sudo systemctl start wireproxy-mullvad
```

### 5. Verify it works

```bash
# Check service status
systemctl status wireproxy-mullvad

# Check which server is active
cat /tmp/wireproxy-active-server.txt

# Test the proxy directly
curl --proxy socks5h://127.0.0.1:1080 https://api.ipify.org
# Should return a non-local IP from the VPN exit country

# Test Polymarket CLOB access through proxy
curl --proxy socks5h://127.0.0.1:1080 https://clob.polymarket.com/health
# Should return {"status":"ok"}
```

### 6. Configure polymarket-cli to use the proxy

Add the proxy to the CLI config file:

```bash
# Edit ~/.config/polymarket/config.json and add/set:
# "proxy": "socks5://127.0.0.1:1080"
```

The CLI reads this automatically — no need to pass `--proxy` or set env vars.

## Troubleshooting

**Service won't start / all configs fail:**
```bash
# Check logs
journalctl -u wireproxy-mullvad -f

# Try running the launcher manually
~/mullvad-wg-configs/wireproxy-launcher.sh
```

**CLOB returns geoblock error:**
```bash
# Check exit IP country
curl --proxy socks5h://127.0.0.1:1080 https://ipinfo.io/json

# If the country is geoblocked, download configs from a different country
```

**Proxy is up but CLI still fails:**
```bash
# Verify proxy setting in config
polymarket -o json wallet show
# Should show proxy in output

# Restart the proxy
sudo systemctl restart wireproxy-mullvad
```

**Mullvad configs expired:**
```bash
# Re-download from https://mullvad.net/en/account/wireguard-config
# Replace files in ~/mullvad-wg-configs/
# Restart: sudo systemctl restart wireproxy-mullvad
```

## Files in this directory

| File | Description |
|------|-------------|
| `SOCKS5-PROXY-SETUP.md` | This file |
| `wireproxy-mullvad.service` | Systemd unit file (copy to `/etc/systemd/system/`) |
| `wireproxy-launcher.sh` | Launcher script — shuffles configs, finds working server, stays alive |
| `example-wg.conf` | Example WireGuard config format (replace with real Mullvad configs) |
