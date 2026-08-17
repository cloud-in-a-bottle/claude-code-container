#!/usr/bin/env bash
# Front-door + tunnel supervisor. openhost routes the app's subdomain to $PORT; we put chisel there
# so external `ssh` can reach an in-container sshd over the same HTTPS subdomain (no extra host ports
# opened). chisel serves the tunnel on the public path and reverse-proxies everything else to the
# Quart app on a loopback backend port, so the browser terminal UI is unaffected.
#
#   external:  chisel client https://<app>.<zone>/_chisel 2222:localhost:22   +   ssh -p 2222 root@localhost
#   in here:   chisel server (:$PORT)  --backend-->  quart (:$BACKEND_PORT)
#                                       --tunnel-->   sshd (127.0.0.1:22)
#
# We fail loudly and exit non-zero if any managed process dies, so openhost restarts the container
# rather than leaving a half-up workbench (e.g. chisel up but the UI backend gone).
set -euo pipefail

PUBLIC_PORT="${PORT:-5000}"                         # what openhost proxies the subdomain to
BACKEND_PORT="${WORKBENCH_BACKEND_PORT:-5001}"      # loopback-only Quart backend
SSH_DIR="$HOME/.ssh"
HOSTKEY="$SSH_DIR/sshd/ssh_host_ed25519_key"        # persisted so the host key survives redeploys
AUTHORIZED_KEYS="$SSH_DIR/authorized_keys"          # user-managed (add your pubkey here); persists
SECRET_KEYS="$SSH_DIR/authorized_keys.secret"       # secret-managed, overwritten from SSH_AUTHORIZED_KEYS each boot
CHISEL_AUTH_FILE="$SSH_DIR/chisel-auth"             # persisted user:pass so the connect string is stable

# --- helpers ----------------------------------------------------------------

# Best-effort fetch of a single secret from the secrets-v2 service (same endpoint remote_services.py
# uses). Prints the value or nothing. Never fails the script — SSH access is optional plumbing.
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

# --- ssh identity + credentials ---------------------------------------------

mkdir -p "$SSH_DIR/sshd" /run/sshd
chmod 700 "$SSH_DIR"
[ -f "$HOSTKEY" ] || ssh-keygen -t ed25519 -f "$HOSTKEY" -N "" -q
touch "$AUTHORIZED_KEYS"
chmod 600 "$AUTHORIZED_KEYS"

# Secret-provided keys are the source of truth for SECRET_KEYS: rewrite it every boot (empty if the
# secret is unset) so removing the secret revokes access. User-managed keys in AUTHORIZED_KEYS are
# independent and never touched here.
seeded_keys="$(fetch_secret SSH_AUTHORIZED_KEYS)"
printf '%s\n' "$seeded_keys" > "$SECRET_KEYS"
chmod 600 "$SECRET_KEYS"

# chisel credential: prefer an operator-set secret, else reuse the persisted one, else mint one.
chisel_auth="$(fetch_secret CHISEL_AUTH)"
if [ -z "$chisel_auth" ]; then
    if [ -s "$CHISEL_AUTH_FILE" ]; then
        chisel_auth="$(cat "$CHISEL_AUTH_FILE")"
    else
        chisel_auth="workbench:$(rand_token)"
    fi
fi
printf '%s' "$chisel_auth" > "$CHISEL_AUTH_FILE"
chmod 600 "$CHISEL_AUTH_FILE"

# ssh logs the user into root's passwd home (/root), but the workbench keeps its real HOME on the
# persistent data dir. Point root's home there so an ssh session lands in the same place as the
# browser terminals (openhost clone, my_project, shell history). Best-effort.
if [ "$HOME" != "/root" ]; then
    usermod -d "$HOME" root 2>/dev/null || true
fi

# --- launch: sshd (loopback), quart backend, chisel front-door --------------

SSHD_BIN="$(command -v sshd || echo /usr/sbin/sshd)"
"$SSHD_BIN" -D -e \
    -f /app/sshd_config \
    -h "$HOSTKEY" \
    -o "AuthorizedKeysFile $AUTHORIZED_KEYS $SECRET_KEYS" \
    -o "PidFile /run/workbench-sshd.pid" &
SSHD_PID=$!

BIND_HOST=127.0.0.1 PORT="$BACKEND_PORT" python3 /app/server.py &
QUART_PID=$!

chisel server \
    --host 0.0.0.0 --port "$PUBLIC_PORT" \
    --backend "http://127.0.0.1:$BACKEND_PORT" \
    --auth "$chisel_auth" &
CHISEL_PID=$!

trap 'kill "$SSHD_PID" "$QUART_PID" "$CHISEL_PID" 2>/dev/null || true' EXIT INT TERM

# If any of the three exits, tear the rest down and exit non-zero so openhost restarts us clean.
wait -n
echo "[tunnel] a managed process exited; shutting down so the container restarts" >&2
exit 1
