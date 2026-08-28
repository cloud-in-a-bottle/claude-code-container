#!/usr/bin/env bash
# Brings up one workspace, then hands the terminal over to Claude. All inputs arrive via env vars
# set by the server -- never string interpolation -- so there is nothing to escape here.
#
#   WS_PATH          absolute path of the workspace dir (already created, empty)
#   WS_REPO          clone URL, kept token-free: this is what origin ends up pointing at
#   WS_MIRROR        absolute path of the project's bare mirror
#   WS_REF           optional branch/tag/sha to check out instead of the default branch
#   WS_SETUP         optional one-off setup command, run in the workspace before Claude
#   WS_GITHUB_TOKEN  optional transient token, used for network git only and never written to disk
#   CLAUDE_BIN, CLAUDE_SESSION_ID
#
# No `set -e`: every failure below still drops the user into a usable terminal in the workspace,
# because a half-made workspace you can inspect beats a tab that vanished.

export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -oBatchMode=yes"

# The token goes into the URL used to reach the network, never into the URL we persist as origin.
AUTHED_URL="$WS_REPO"
if [ -n "${WS_GITHUB_TOKEN:-}" ]; then
    ENCODED_TOKEN="$(
        python3 -c 'import sys, urllib.parse; sys.stdout.write(urllib.parse.quote(sys.argv[1], safe=""))' \
            "$WS_GITHUB_TOKEN"
    )"
    case "$WS_REPO" in
        https://*) AUTHED_URL="https://${ENCODED_TOKEN}@${WS_REPO#https://}" ;;
        http://*)  AUTHED_URL="http://${ENCODED_TOKEN}@${WS_REPO#http://}" ;;
    esac
    unset ENCODED_TOKEN
fi
# Drop it from the environment before any `exec bash`, so it can't leak into the user's shell.
unset WS_GITHUB_TOKEN

echo
if [ -d "$WS_MIRROR" ]; then
    echo "[workbench] updating mirror for $WS_REPO"
    git --git-dir="$WS_MIRROR" remote set-url origin "$AUTHED_URL"
    git --git-dir="$WS_MIRROR" fetch --prune origin '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*' \
        || echo "[workbench] mirror fetch failed; using the copy already on disk." >&2
    # Leave no token behind in the mirror's config.
    git --git-dir="$WS_MIRROR" remote set-url origin "$WS_REPO"
else
    echo "[workbench] mirroring $WS_REPO (first workspace for this project)"
    mkdir -p "$(dirname "$WS_MIRROR")"
    if git clone --mirror -- "$AUTHED_URL" "$WS_MIRROR"; then
        git --git-dir="$WS_MIRROR" remote set-url origin "$WS_REPO"
    else
        echo "[workbench] mirror clone failed; dropping you into a shell." >&2
        rm -rf "$WS_MIRROR"
        cd "$WS_PATH" 2>/dev/null || cd "$HOME" || exit 1
        exec bash -l
    fi
fi

# Local clone from the mirror: hardlinked objects, so this is fast and near-free on disk however
# many workspaces a project has.
echo "[workbench] creating workspace at $WS_PATH"
if ! git clone -- "$WS_MIRROR" "$WS_PATH"; then
    echo "[workbench] workspace clone failed; dropping you into a shell." >&2
    cd "$WS_PATH" 2>/dev/null || cd "$HOME" || exit 1
    exec bash -l
fi
cd "$WS_PATH" || exit 1
# origin points at the mirror after that clone; repoint it at the real remote so push/pull and
# anything reading the remote URL behave the way they would in an ordinary checkout.
git remote set-url origin "$WS_REPO"

if [ -n "${WS_REF:-}" ]; then
    echo "[workbench] checking out $WS_REF"
    git checkout "$WS_REF" \
        || echo "[workbench] checkout of $WS_REF failed; staying on the default branch." >&2
fi

if [ -n "${WS_SETUP:-}" ]; then
    echo
    echo "[workbench] running project setup: $WS_SETUP"
    bash -lc "$WS_SETUP" || echo "[workbench] setup command failed; continuing anyway." >&2
fi

echo
# Create the pinned session, or rejoin it if a crashed earlier attempt already made it -- claude
# rejects --session-id for an id that exists.
for _i in 1 2 3; do
    { "${CLAUDE_BIN}" --session-id "${CLAUDE_SESSION_ID}" --dangerously-skip-permissions ||
        "${CLAUDE_BIN}" --resume "${CLAUDE_SESSION_ID}" --dangerously-skip-permissions; } && break
    sleep 1
done
exec bash -l
