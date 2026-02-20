# browser-dom — OpenClaw Plugin

> Direct DOM manipulation tools for browser automation via CDP (Chrome DevTools Protocol).
> Eliminates stale-ref problems on dynamic sites (Deliveroo, Amazon, etc.) by using CSS selectors, XPath, and text matching instead of snapshot refs.

---

## Architecture

```
                 OpenClaw Gateway (Node.js, systemd)
                       |
                       | loads plugin via jiti
                       v
                 browser-dom plugin
                       |
                       | direct CDP (WebSocket + HTTP)
                       v
               Chrome/Chromium (headless, systemd)
               :18800 (CDP port)
               user-data: ~/.openclaw/browser/openclaw/user-data
```

The plugin communicates **directly with Chrome via CDP** (Chrome DevTools Protocol), bypassing OpenClaw's internal browser control service entirely. This is necessary because:
- OpenClaw's browser control runs in embedded/in-memory mode (no HTTP server)
- The internal `fetchBrowserJson` function is tree-shaken from the bundled distribution
- CDP direct is faster, simpler, and has zero dependencies on OpenClaw internals

### Transport Layer

- **HTTP** to `http://127.0.0.1:18800/json/list` for target discovery
- **WebSocket** to Chrome's debugger URL for `Runtime.evaluate`, `Page.navigate`, `Page.captureScreenshot`
- Uses Node 22 native `WebSocket` (no external packages needed)

---

## Tools Registered (8)

| Tool | Description |
|------|-------------|
| `dom_navigate` | Navigate to URL + wait for page ready (selector or networkidle) |
| `dom_click` | Click via CSS selector / XPath / visible text (auto-waits) |
| `dom_fill` | Fill inputs via CSS selector (React-compatible, native value setter) |
| `dom_wait` | Wait for selector / text / textGone / URL / loadState / JS condition |
| `dom_extract` | Extract text / attributes from elements (single or all matches) |
| `dom_screenshot` | Screenshot full page, viewport, or specific element (saved to /tmp) |
| `dom_query` | Discover elements on the page (inspect DOM structure before acting) |
| `dom_evaluate` | Execute arbitrary JavaScript in page context |

All tools use the `dom_` prefix to coexist with the built-in `browser` tool. They are registered as `optional: true`, so the agent picks them when appropriate.

---

## Prerequisites

1. **Chrome/Chromium** installed on the host (`google-chrome` or `chromium-browser`)
2. **Chrome headless service** running on CDP port 18800 (see below)
3. **OpenClaw gateway** running with the plugin enabled
4. **Node.js 22+** (for native WebSocket support)

---

## Installation

### 1. Copy plugin to extensions directory

```bash
# From the JARVIS repo root
cp -r extensions/browser-dom ~/.openclaw/extensions/browser-dom
cd ~/.openclaw/extensions/browser-dom
npm install
```

### 2. Install and enable the Chrome headless systemd service

```bash
sudo cp /opt/jarvis/extensions/browser-dom/openclaw-chrome.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-chrome
```

Verify Chrome is running:
```bash
curl -s http://127.0.0.1:18800/json/version | head -3
# Should show: { "Browser": "Chrome/xxx", ... }
```

### 3. Configure OpenClaw

Add to `~/.openclaw/openclaw.json`:

```json
{
  "browser": {
    "enabled": true,
    "evaluateEnabled": true,
    "defaultProfile": "openclaw",
    "profiles": {
      "openclaw": {
        "cdpPort": 18800,
        "color": "#4A90D9"
      }
    }
  },
  "plugins": {
    "entries": {
      "browser-dom": {
        "enabled": true,
        "config": {
          "cdpUrl": "http://127.0.0.1:18800",
          "defaultTimeoutMs": 15000
        }
      }
    }
  },
  "tools": {
    "alsoAllow": ["group:plugins"]
  }
}
```

### 4. Restart OpenClaw

```bash
sudo systemctl restart openclaw
```

Verify the plugin is loaded:
```bash
journalctl -u openclaw --no-pager -n 20 | grep browser-dom
# Should show: [browser-dom] Registering DOM tools -> CDP http://127.0.0.1:18800
# Should show: [browser-dom] 8 DOM tools registered successfully.
```

---

## Systemd Services

### openclaw-chrome.service

Manages a headless Chrome instance with a dedicated user-data directory for the `openclaw` profile.

```ini
[Unit]
Description=OpenClaw Headless Chrome (CDP :18800)
After=network.target
Before=openclaw.service

[Service]
Type=simple
User=jarvis
ExecStartPre=/bin/rm -f /home/jarvis/.openclaw/browser/openclaw/user-data/SingletonLock
ExecStart=/usr/bin/google-chrome \
    --headless=new \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --remote-debugging-port=18800 \
    --remote-debugging-address=127.0.0.1 \
    --user-data-dir=/home/jarvis/.openclaw/browser/openclaw/user-data \
    about:blank
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Key details:
- **ExecStartPre**: removes stale `SingletonLock` from previous crashes / `pkill chrome`
- **--headless=new**: uses Chrome's new headless mode (full browser, not old headless)
- **--no-sandbox**: required for running as non-root in containers/VMs
- **--disable-dev-shm-usage**: prevents `/dev/shm` exhaustion in memory-constrained environments
- **--user-data-dir**: isolated profile directory (cookies, localStorage, etc.)
- **Before=openclaw.service**: ensures Chrome is up before OpenClaw starts
- **Restart=on-failure**: auto-restarts if Chrome crashes

### Boot order

```
1. openclaw-chrome.service  (Chrome headless on :18800)
2. openclaw.service          (OpenClaw gateway on :18789, loads browser-dom plugin)
```

---

## Troubleshooting

### Plugin shows "fetch failed" or "timed out"

Chrome is not running or not reachable on port 18800:
```bash
# Check Chrome service
sudo systemctl status openclaw-chrome
journalctl -u openclaw-chrome --no-pager -n 20

# Check CDP is responding
curl -s http://127.0.0.1:18800/json/version

# If Chrome crashed, restart it
sudo systemctl restart openclaw-chrome
```

### "No browser page found"

Chrome is running but has no page targets:
```bash
curl -s http://127.0.0.1:18800/json/list
# Should show at least one target with type: "page"
# If empty, Chrome may have crashed internally. Restart:
sudo systemctl restart openclaw-chrome
```

### Agent doesn't use dom_* tools

The agent may prefer the built-in `browser` tool. To encourage dom_* usage:
1. Verify tools are registered: check `journalctl -u openclaw | grep browser-dom`
2. Ensure `tools.alsoAllow` includes `"group:plugins"` in `openclaw.json`
3. The agent will choose dom_* tools when the prompt mentions CSS selectors, XPath, or direct DOM manipulation

### Chrome uses too much memory

Headless Chrome typically uses 100-300 MB. If it grows:
```bash
# Check memory usage
ps aux | grep chrome | grep -v grep

# Restart to free memory
sudo systemctl restart openclaw-chrome
```

### Cookies / session not persisting

Cookies are stored in the user-data directory. If Chrome is restarted cleanly (via systemd), cookies persist. If killed with `pkill chrome`, the SingletonLock cleanup in ExecStartPre handles the stale lock.

To check cookie storage:
```bash
ls -la ~/.openclaw/browser/openclaw/user-data/Default/Cookies
```

---

## Development

### File Structure

```
extensions/browser-dom/
  index.ts                    # Plugin entry point (registers 8 tools)
  openclaw.plugin.json        # Plugin manifest (config schema)
  package.json                # Dependencies (ws as backup)
  tsconfig.json               # TypeScript config
  openclaw-chrome.service     # Systemd unit file for Chrome
  src/
    dom-engine.ts             # Core engine: CDP transport + DOM operations
    tools.ts                  # Agent tool definitions (TypeBox schemas)
```

### Testing

```bash
# Quick CDP connectivity test
curl -s http://127.0.0.1:18800/json/version

# Test via agent (local run)
openclaw agent --local -m 'Use dom_navigate to go to https://example.com, then dom_extract the title element' --to test --json

# Full Deliveroo test
openclaw agent --local -m 'Vai su deliveroo.it e dimmi cosa vedi nella homepage' --to test --json
```

---

## Ports Reference

| Port | Service | Protocol | Purpose |
|------|---------|----------|---------|
| 18800 | Chrome CDP | HTTP + WebSocket | DevTools Protocol (target discovery + commands) |
| 18789 | OpenClaw Gateway | WebSocket | Gateway RPC (loads browser-dom plugin) |
