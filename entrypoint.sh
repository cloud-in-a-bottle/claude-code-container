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

# Link every bundled skill into the user's skill dir, replacing whatever is already there. $HOME
# persists across rebuilds, so without the replace an old link (or a directory shadowing the name)
# would pin an existing workbench to whatever it first saw. Bundled skill names are therefore
# reserved: to customise one, copy it to a different name.
mkdir -p "$HOME/.claude/skills"
for skill_src in /app/skills/*/; do
    [ -d "$skill_src" ] || continue
    skill_dst="$HOME/.claude/skills/$(basename "$skill_src")"
    rm -rf "$skill_dst"
    # -n so an existing symlink is replaced, not dereferenced into its target directory.
    ln -sfn "${skill_src%/}" "$skill_dst"
done

export OPENHOST_DIR
exec python3 /app/server.py
