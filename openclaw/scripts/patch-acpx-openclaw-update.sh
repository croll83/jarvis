#!/usr/bin/env bash
# ============================================================================
# patch-acpx-openclaw-update.sh
#
# Two-part patch for the acpx/openclaw ACP stack:
#
# PART 1 — Process Group Kill (acpx CLI)
#   Patches the bundled acpx CLI to kill the entire process group (agent +
#   grandchildren such as MCP servers) when an ACP session's queue-owner
#   terminates, instead of only killing the direct child PID.
#   Without this patch, orphaned grandchild processes accumulate and
#   eventually exhaust system memory.
#
# PART 2 — ACP Session Persistence (gateway bundles)
#   Patches the OpenClaw gateway bundles to generate deterministic ACP
#   session keys derived from the parent session + target agent (SHA-256),
#   instead of a fresh crypto.randomUUID() on every interaction.
#   Without this patch, every ACP message creates a brand-new session,
#   losing conversation history and wasting resources.
#
# NOTE: Patches 3-5 (llm-task shim, sessionKey, promptMode) were removed.
#   The trading daemon now uses LLMDirectClient which calls provider APIs
#   directly (Anthropic OAuth, Google, xAI) without going through OpenClaw's
#   embedded agent runner. Those patches are no longer needed.
#
# Usage:
#   ./patch-acpx-openclaw-update.sh          # auto-detect and apply all
#   ./patch-acpx-openclaw-update.sh --check  # verify patch status
#
# Designed to be re-run safely after openclaw updates (idempotent).
# ============================================================================
set -euo pipefail

OPENCLAW_BASE="/home/linuxbrew/.linuxbrew/lib/node_modules/openclaw"
BACKUP_DIR="/home/jarvis/acpx-patch-backups"

# ---------------------------------------------------------------------------
# Locate the acpx cli.js file
# ---------------------------------------------------------------------------
find_cli_js() {
  local candidates=(
    "$OPENCLAW_BASE/extensions/acpx/node_modules/acpx/dist/cli.js"
  )
  local session_file
  session_file=$(find "$OPENCLAW_BASE/extensions/acpx/node_modules/acpx/dist/" \
    -maxdepth 1 -name 'session-*.js' 2>/dev/null | head -1) || true
  if [[ -n "$session_file" ]]; then
    candidates+=("$session_file")
  fi

  for f in "${candidates[@]}"; do
    if [[ -f "$f" ]] && grep -q 'terminateAgentProcess' "$f" 2>/dev/null; then
      echo "$f"
      return 0
    fi
  done
  echo ""
  return 1
}

get_acpx_version() {
  local pkg="$OPENCLAW_BASE/extensions/acpx/node_modules/acpx/package.json"
  if [[ -f "$pkg" ]]; then
    grep '"version"' "$pkg" | head -1 | sed 's/.*"version".*"\(.*\)".*/\1/'
  else
    echo "unknown"
  fi
}

is_patched() {
  local file="$1"
  if grep -q 'detached: true' "$file" && \
     grep -q 'process\.kill(-child\.pid' "$file"; then
    return 0
  fi
  return 1
}

verify_patchable() {
  local file="$1"
  local errors=0
  if ! grep -q 'stdio: \["pipe", "pipe", "pipe"\]' "$file"; then
    echo "  ERROR: Cannot find agent spawn stdio pattern" >&2
    errors=$((errors + 1))
  fi
  if ! grep -q 'child\.kill("SIGTERM")' "$file"; then
    echo "  ERROR: Cannot find child.kill(\"SIGTERM\") pattern" >&2
    errors=$((errors + 1))
  fi
  if ! grep -q 'child\.kill("SIGKILL")' "$file"; then
    echo "  ERROR: Cannot find child.kill(\"SIGKILL\") pattern" >&2
    errors=$((errors + 1))
  fi
  return $errors
}

apply_patch() {
  local file="$1"
  local version
  version=$(get_acpx_version)

  echo "Patching acpx v${version}: ${file}"

  mkdir -p "$BACKUP_DIR"
  local backup_name
  backup_name="cli.js.orig-${version}-$(date +%Y%m%d-%H%M%S)"
  cp "$file" "${BACKUP_DIR}/${backup_name}"
  echo "  Backup: ${BACKUP_DIR}/${backup_name}"

  sed -i 's|stdio: \["pipe", "pipe", "pipe"\]|stdio: ["pipe", "pipe", "pipe"], detached: true|' "$file"
  echo "  [1/3] Added detached: true to agent spawn"

  sed -i 's|child\.kill("SIGTERM")|process.kill(-child.pid, "SIGTERM")|' "$file"
  echo "  [2/3] Replaced SIGTERM with process group kill"

  sed -i 's|child\.kill("SIGKILL")|process.kill(-child.pid, "SIGKILL")|' "$file"
  echo "  [3/3] Replaced SIGKILL with process group kill"

  echo "  Done."
}

# ===========================================================================
# PART 2 — ACP Session Persistence (gateway bundles)
# ===========================================================================

find_gateway_bundles() {
  local dist_dir="$OPENCLAW_BASE/dist"
  [[ -d "$dist_dir" ]] || return 1
  grep -rl 'agent:.\{1,\}:acp:.\{1,\}randomUUID()' "$dist_dir" 2>/dev/null || true
}

is_session_patched() {
  local file="$1"
  grep -q 'createHash("sha256").update(parentSessionKey' "$file" 2>/dev/null
}

verify_session_patchable() {
  local file="$1"
  local errors=0
  if ! grep -q 'agent:${targetAgentId}:acp:${crypto\.randomUUID()}' "$file" 2>/dev/null; then
    echo "  WARN: Cannot find sessions_spawn pattern in $(basename "$file")" >&2
    errors=$((errors + 1))
  fi
  if ! grep -q 'agent:${spawn\.agentId}:acp:${randomUUID()}' "$file" 2>/dev/null; then
    echo "  WARN: Cannot find acp_spawn pattern in $(basename "$file")" >&2
    errors=$((errors + 1))
  fi
  return $errors
}

apply_session_patch() {
  local file="$1"
  local basename
  basename=$(basename "$file")

  echo "Patching session persistence: ${basename}"

  mkdir -p "$BACKUP_DIR"
  local backup_name
  backup_name="${basename}.orig-$(date +%Y%m%d-%H%M%S)"
  cp "$file" "${BACKUP_DIR}/${backup_name}"
  echo "  Backup: ${BACKUP_DIR}/${backup_name}"

  local patched_a=0
  local patched_b=0

  if grep -q 'agent:${targetAgentId}:acp:${crypto\.randomUUID()}' "$file"; then
    sed -i 's|`agent:${targetAgentId}:acp:${crypto\.randomUUID()}`|`agent:${targetAgentId}:acp:${parentSessionKey ? crypto.createHash("sha256").update(parentSessionKey+":"+targetAgentId).digest("hex").slice(0,32) : crypto.randomUUID()}`|g' "$file"
    patched_a=1
    echo "  [A] Patched sessions_spawn: deterministic key from parentSessionKey"
  fi

  if grep -q 'agent:${spawn\.agentId}:acp:${randomUUID()}' "$file"; then
    sed -i 's|`agent:${spawn\.agentId}:acp:${randomUUID()}`|`agent:${spawn.agentId}:acp:${params.sessionKey ? crypto.createHash("sha256").update(params.sessionKey+":"+spawn.agentId).digest("hex").slice(0,32) : randomUUID()}`|g' "$file"
    patched_b=1
    echo "  [B] Patched acp_spawn: deterministic key from params.sessionKey"
  fi

  if [[ $patched_a -eq 0 && $patched_b -eq 0 ]]; then
    echo "  SKIP: No matching patterns found in ${basename}"
    return 1
  fi

  echo "  Done."
  return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  if [[ "${1:-}" == "--check" ]]; then
    echo "=== Part 1: Process Group Kill (acpx CLI) ==="
    local target_file
    target_file=$(find_cli_js || true)
    if [[ -z "$target_file" ]]; then
      echo "  ERROR: Cannot locate acpx cli.js" >&2
    elif is_patched "$target_file"; then
      echo "  PATCHED (acpx v$(get_acpx_version))"
    else
      echo "  NOT PATCHED (acpx v$(get_acpx_version))"
    fi

    echo ""
    echo "=== Part 2: ACP Session Persistence (gateway) ==="
    local bundles
    bundles=$(find_gateway_bundles)
    if [[ -z "$bundles" ]]; then
      echo "  No gateway bundles found."
    else
      while IFS= read -r bundle; do
        if is_session_patched "$bundle"; then
          echo "  PATCHED: $(basename "$bundle")"
        else
          echo "  NOT PATCHED: $(basename "$bundle")"
        fi
      done <<< "$bundles"
    fi

    echo ""
    echo "=== Patches 3-5: REMOVED ==="
    echo "  Trading daemon uses LLMDirectClient (direct API calls)."
    echo "  No longer depends on OpenClaw embedded runner for LLM calls."
    exit 0
  fi

  local overall_changed=0

  echo "=== Part 1: Process Group Kill (acpx CLI) ==="
  local target_file
  target_file=$(find_cli_js || true)

  if [[ -z "$target_file" || ! -f "$target_file" ]]; then
    echo "  SKIP: Cannot locate acpx cli.js" >&2
  elif is_patched "$target_file"; then
    echo "  Already patched. Skipping."
  elif verify_patchable "$target_file"; then
    apply_patch "$target_file"
    overall_changed=1
  else
    echo "  ERROR: File structure does not match expected patterns." >&2
  fi

  echo ""

  echo "=== Part 2: ACP Session Persistence (gateway) ==="
  local bundles
  bundles=$(find_gateway_bundles)

  if [[ -z "$bundles" ]]; then
    echo "  SKIP: No gateway bundles found."
  else
    while IFS= read -r bundle; do
      if is_session_patched "$bundle"; then
        echo "  Already patched: $(basename "$bundle"). Skipping."
      elif verify_session_patchable "$bundle"; then
        apply_session_patch "$bundle" && overall_changed=1
      else
        echo "  SKIP: $(basename "$bundle") — patterns don't match."
      fi
    done <<< "$bundles"
  fi

  echo ""
  if [[ $overall_changed -eq 1 ]]; then
    echo "=== IMPORTANT ==="
    echo "Restart the gateway to activate the patches:"
    echo "  systemctl --user restart openclaw-gateway"
  else
    echo "All patches already applied. Nothing to do."
  fi
}

main "$@"
