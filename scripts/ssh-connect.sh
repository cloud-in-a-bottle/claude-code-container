#!/usr/bin/env bash
# Connect to a claude-workbench over its chisel tunnel, from your own machine.
#
# Prereqs on THIS machine: `chisel` (https://github.com/jpillora/chisel) and `ssh`.
# Prereqs in the workbench: your ssh public key in ~/.ssh/authorized_keys (add it in a terminal
# tab, or set the SSH_AUTHORIZED_KEYS secret). Get the chisel credential from the workbench's
# terminal login banner (or the CHISEL_AUTH secret you set).
#
# Usage:
#   CHISEL_AUTH='workbench:xxxxx' ./ssh-connect.sh https://claude-workbench.<zone>
#   CHISEL_AUTH='workbench:xxxxx' ./ssh-connect.sh https://claude-workbench.<zone> 2222   # custom local port
#
# It opens the tunnel, then drops you into an ssh session as root. Ctrl-D / exit tears the tunnel down.
set -euo pipefail

URL="${1:-}"
LOCAL_PORT="${2:-2222}"

if [ -z "$URL" ]; then
    echo "usage: CHISEL_AUTH='user:pass' $0 https://claude-workbench.<zone> [local_port]" >&2
    exit 2
fi
if [ -z "${CHISEL_AUTH:-}" ]; then
    echo "error: set CHISEL_AUTH='user:pass' (from the workbench login banner)" >&2
    exit 2
fi
command -v chisel >/dev/null || { echo "error: chisel not installed — see https://github.com/jpillora/chisel" >&2; exit 1; }

# Tunnel local_port -> the workbench's own localhost:22 (sshd). The /_chisel path is the openhost
# public route that reaches chisel without a browser login.
chisel client --auth "$CHISEL_AUTH" "${URL%/}/_chisel" "${LOCAL_PORT}:localhost:22" &
CHISEL_PID=$!
trap 'kill "$CHISEL_PID" 2>/dev/null || true' EXIT

# Wait for the local forward to come up.
for _ in $(seq 1 50); do
    if (exec 3<>"/dev/tcp/127.0.0.1/${LOCAL_PORT}") 2>/dev/null; then exec 3>&- 3<&-; break; fi
    sleep 0.2
done

echo "tunnel up on 127.0.0.1:${LOCAL_PORT} -> workbench sshd. connecting..." >&2
# Skip host-key nagging: identity is already established by the chisel credential + your ssh key,
# and the workbench host key is regenerated only if its persistent data dir is wiped.
ssh -p "$LOCAL_PORT" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    root@localhost
