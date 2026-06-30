#!/usr/bin/env bash
set -euo pipefail

# If the openhost runtime has provisioned a persistent data dir for us, move
# HOME there so `claude login` credentials, the openhost clone, shell history,
# etc. survive container redeploys. The site rcfile lives at
# /etc/profile.d/workbench.sh, so HOME is left untouched — anything the user
# writes into ~/.bashrc / ~/.bash_profile is theirs and survives image updates.
if [ -n "${OPENHOST_APP_DATA_DIR:-}" ]; then
    export HOME="$OPENHOST_APP_DATA_DIR/home"
    mkdir -p "$HOME"
    cd "$HOME"
fi

OPENHOST_REPO="${OPENHOST_REPO_URL:-https://github.com/imbue-openhost/openhost.git}"
OPENHOST_DIR="${OPENHOST_DIR:-$HOME/openhost}"
SKILL_SRC="/app/skills/openhost"
SKILL_DST="$HOME/.claude/skills/openhost"

if [ ! -d "$OPENHOST_DIR/.git" ]; then
    echo "[entrypoint] cloning openhost into $OPENHOST_DIR ..."
    git clone --depth 1 "$OPENHOST_REPO" "$OPENHOST_DIR" || \
        echo "[entrypoint] WARN: openhost clone failed; you can clone manually later."
fi

APP_TEMPLATE_DIR="$HOME/app-template"
if [ ! -d "$APP_TEMPLATE_DIR/.git" ]; then
    echo "[entrypoint] cloning app-template into $APP_TEMPLATE_DIR ..."
    git clone --depth 1 https://github.com/imbue-openhost/app-template.git "$APP_TEMPLATE_DIR" || \
        echo "[entrypoint] WARN: app-template clone failed; you can clone manually later."
fi

mkdir -p "$HOME/my_project"

mkdir -p "$(dirname "$SKILL_DST")"
if [ ! -e "$SKILL_DST" ]; then
    ln -s "$SKILL_SRC" "$SKILL_DST"
fi

export OPENHOST_DIR
exec python3 /app/server.py
