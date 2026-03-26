# Building polymarket-cli on Ubuntu 24.04

Build from source using our patched fork which adds SOCKS5 proxy support and gnosis-safe wallet derivation fix.

## Prerequisites

```bash
# Build tools
sudo apt-get update && sudo apt-get install -y build-essential

# Rust toolchain — Rust 1.88+ required (edition 2024)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env
rustup update stable
```

## First-time install

```bash
# Clone our fork (has SOCKS5 proxy support + gnosis-safe fix)
git clone https://github.com/croll83/polymarket-cli.git /tmp/polymarket-cli
cd /tmp/polymarket-cli
git checkout feat/socks-proxy-support

# Build and install (release profile, ~2-3 min)
cargo install --path .

# Verify
polymarket --version
# → polymarket 0.1.4

# Clean up build dir
rm -rf /tmp/polymarket-cli
```

The binary is installed to `~/.cargo/bin/polymarket`.

## Updating to a new version

When our fork has new commits:

```bash
git clone https://github.com/croll83/polymarket-cli.git /tmp/polymarket-cli
cd /tmp/polymarket-cli
git checkout feat/socks-proxy-support
cargo install --path . --force
rm -rf /tmp/polymarket-cli

polymarket --version
```

If upstream merged our SOCKS5 PR, switch to the official repo:
```bash
git clone https://github.com/Polymarket/polymarket-cli.git /tmp/polymarket-cli
cd /tmp/polymarket-cli
cargo install --path . --force
rm -rf /tmp/polymarket-cli
```

## Syncing our fork with upstream releases

When upstream releases a new version and our fork needs to catch up:

```bash
git clone https://github.com/croll83/polymarket-cli.git ~/polymarket-cli-dev
cd ~/polymarket-cli-dev
git checkout feat/socks-proxy-support

# Add upstream remote and rebase
git remote add upstream https://github.com/Polymarket/polymarket-cli.git
git fetch upstream
git rebase upstream/main

# Resolve any conflicts, then force-push
git push origin feat/socks-proxy-support --force

# Rebuild
cargo install --path . --force
```

## What our fork adds (on top of upstream)

Branch `feat/socks-proxy-support`:

1. **`--proxy` flag + `POLYMARKET_PROXY` env var** — pass a SOCKS5 or HTTP proxy URL
2. **Config file proxy field** — add `"proxy": "socks5://127.0.0.1:1080"` to `~/.config/polymarket/config.json`
3. **Thread-safe env vars + NO_PROXY** — `set_var` runs before tokio runtime; alloy RPC excluded via `NO_PROXY` (reqwest 0.12 has no socks support)
4. **Trading wallet derivation fix** — `wallet show/create/import` now respects `signature_type` from config (`gnosis-safe` → Gnosis Safe address, `proxy` → EIP-1167 proxy address)

Proxy resolution priority: `--proxy` flag > `POLYMARKET_PROXY` env > config file `proxy` field.

## Upstream PR

<https://github.com/Polymarket/polymarket-cli/pull/21>

Once merged, build from `main` on the official repo instead.
