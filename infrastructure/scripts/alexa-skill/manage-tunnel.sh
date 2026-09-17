#!/usr/bin/env bash
# =============================================================================
# Gestione tunnel Cloudflare "wagmi-alexa" via API
# =============================================================================
# Crea/aggiorna il tunnel, l'ingress (2 route) e i record DNS, e mostra stato.
# Credenziali via env o .env (vedi .env.example). NON committare .env.
#
# Uso:
#   ./manage-tunnel.sh status                 # stato tunnel + ingress
#   ./manage-tunnel.sh set-haos-ip 192.168.68.97   # aggiorna backend route2
#   ./manage-tunnel.sh token                  # stampa il connector token
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
[[ -f .env ]] && { set -a; source .env; set +a; }

: "${CF_API_KEY:?CF_API_KEY (Global API Key) non impostata}"
: "${CF_EMAIL:?CF_EMAIL non impostata}"
: "${CF_ACCOUNT_ID:=4364c2f1fb2e84ef0476582887159b37}"
: "${CF_ZONE:=mintwork.it}"
: "${TUNNEL_NAME:=wagmi-alexa}"
: "${HOST_ALEXA:=wagmialexa}"
: "${HOST_STREAM:=wagmialexastream}"

api() { curl -s --max-time 25 -H "X-Auth-Email: $CF_EMAIL" -H "X-Auth-Key: $CF_API_KEY" -H "Content-Type: application/json" "$@"; }
B="https://api.cloudflare.com/client/v4"

tunnel_id() {
  api "$B/accounts/$CF_ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME&is_deleted=false" \
   | python3 -c 'import sys,json;r=json.load(sys.stdin).get("result",[]);print(r[0]["id"] if r else "")'
}

case "${1:-status}" in
  status)
    TID=$(tunnel_id); echo "tunnel_id: $TID"
    api "$B/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TID" \
     | python3 -c 'import sys,json;r=json.load(sys.stdin)["result"];print("status:",r.get("status"),"conns:",len(r.get("connections") or []))'
    echo "ingress:"; api "$B/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TID/configurations" \
     | python3 -c 'import sys,json;[print(" ",i.get("hostname","<catchall>"),"->",i["service"]) for i in json.load(sys.stdin)["result"]["config"]["ingress"]]'
    ;;
  set-haos-ip)
    IP="${2:?uso: set-haos-ip <ip>}"; TID=$(tunnel_id)
    api -X PUT "$B/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TID/configurations" \
     --data "{\"config\":{\"ingress\":[{\"hostname\":\"$HOST_ALEXA.$CF_ZONE\",\"service\":\"http://127.0.0.1:5000\"},{\"hostname\":\"$HOST_STREAM.$CF_ZONE\",\"service\":\"http://$IP:8097\"},{\"service\":\"http_status:404\"}]}}" \
     | python3 -c 'import sys,json;print("ok" if json.load(sys.stdin).get("success") else "errore")'
    ;;
  token)
    TID=$(tunnel_id)
    api "$B/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TID/token" | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"])'
    ;;
  *) echo "comandi: status | set-haos-ip <ip> | token"; exit 1;;
esac
