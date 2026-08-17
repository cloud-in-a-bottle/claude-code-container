#!/usr/bin/env bash
# Rotate the generated chisel credential for SSH-over-chisel access.
#
# Run inside a workbench terminal. This writes a new ~/.ssh/chisel-auth token and restarts the
# workbench by terminating the chisel server; tunnel.sh will exit, and openhost will restart the
# container with the new credential. Your current browser terminal will disconnect.
set -euo pipefail

SSH_DIR="${HOME:?}/.ssh"
CHISEL_AUTH_FILE="$SSH_DIR/chisel-auth"

fetch_secret() {
    local key="$1"
    [ -n "${OPENHOST_ROUTER_URL:-}" ] && [ -n "${OPENHOST_APP_TOKEN:-}" ] || return 0
    curl -fsS --max-time 5 -X POST \
        -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"keys\":[\"$key\"]}" \
        "$OPENHOST_ROUTER_URL/api/services/v2/call/secrets/get" 2>/dev/null \
        | jq -r --arg k "$key" '.secrets[$k] // empty' 2>/dev/null || true
}

rand_token() {
    head -c 18 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 24
}

if [ -n "$(fetch_secret CHISEL_AUTH)" ]; then
    cat >&2 <<'EOF'
CHISEL_AUTH is set in the secrets app. That secret overrides ~/.ssh/chisel-auth on every start.
Rotate or remove the CHISEL_AUTH secret in the secrets app, then restart this workbench.
EOF
    exit 1
fi

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
new_auth="workbench:$(rand_token)"
printf '%s' "$new_auth" > "$CHISEL_AUTH_FILE"
chmod 600 "$CHISEL_AUTH_FILE"

cat <<EOF
Rotated chisel credential.
New local-side command after this workbench restarts:

  chisel client --auth '$new_auth' \
    https://${OPENHOST_APP_NAME:-claude-workbench}.${OPENHOST_ZONE_DOMAIN:-<zone>}/_chisel 2222:localhost:22 &
  ssh -p 2222 root@localhost

Restarting workbench now; this terminal will disconnect.
EOF

sleep 2
if command -v pkill >/dev/null 2>&1; then
    pkill -TERM -f 'chisel server' || true
else
    # Fallback for minimal images without pkill.
    pids="$(ps -eo pid=,args= | awk '/[c]hisel server/ {print $1}')"
    [ -z "$pids" ] || kill $pids || true
fi
