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
APP_TEMPLATE_DIR="$HOME/app-template"
WORKBENCH_REPO="${WORKBENCH_REPO_URL:-https://github.com/cloud-in-a-bottle/claude-code-container.git}"
WORKBENCH_DIR="${WORKBENCH_DIR:-$HOME/claude-code-container}"
IMAGE_STAMP="/app/.image-stamp"
# Kept inside .git so `git clean` can't remove it and it never shows up as an untracked file.
CHECKOUT_STAMP="$WORKBENCH_DIR/.git/openhost-image-stamp"

seed_checkouts() {
    if [ ! -d "$OPENHOST_DIR/.git" ]; then
        echo "[entrypoint] cloning openhost into $OPENHOST_DIR ..."
        git clone --depth 1 "$OPENHOST_REPO" "$OPENHOST_DIR" || \
            echo "[entrypoint] WARN: openhost clone failed; you can clone manually later."
    fi

    if [ ! -d "$APP_TEMPLATE_DIR/.git" ]; then
        echo "[entrypoint] cloning app-template into $APP_TEMPLATE_DIR ..."
        git clone --depth 1 https://github.com/imbue-openhost/app-template.git "$APP_TEMPLATE_DIR" || \
            echo "[entrypoint] WARN: app-template clone failed; you can clone manually later."
    fi

    # A checkout of the workbench's own source, so you can hack on the app you're sitting in.
    # Tracks the repo's default branch, which may be ahead of the image actually running.
    #
    # Edits here are not durable. /app/.image-stamp changes whenever the image is rebuilt, so a
    # rebuild -- i.e. an externally initiated app update -- resets this checkout to the remote and
    # discards local work. An ordinary container restart reuses the same image, leaves the stamp
    # alone, and so leaves your edits alone.
    #
    # Every step is guarded: a workbench that can't reach GitHub, or whose checkout is in some
    # state git dislikes, must still start.
    workbench_synced=yes
    if [ ! -d "$WORKBENCH_DIR/.git" ]; then
        echo "[entrypoint] cloning claude-code-container into $WORKBENCH_DIR ..."
        echo "[entrypoint] (edits there are discarded when the app is updated)"
        git clone --depth 1 "$WORKBENCH_REPO" "$WORKBENCH_DIR" || {
            echo "[entrypoint] WARN: workbench clone failed; you can clone manually later."
            workbench_synced=no
        }
    elif [ -f "$IMAGE_STAMP" ] && ! cmp -s "$IMAGE_STAMP" "$CHECKOUT_STAMP"; then
        echo "[entrypoint] app updated — resetting $WORKBENCH_DIR to the remote, discarding local edits"
        (
            cd "$WORKBENCH_DIR" &&
            git fetch --depth 1 origin HEAD &&
            git reset --hard FETCH_HEAD &&
            git clean -fd
        ) || {
            echo "[entrypoint] WARN: could not resync $WORKBENCH_DIR; leaving it as it is."
            workbench_synced=no
        }
    fi

    # Only record the stamp once the checkout really matches the image. Advancing it after a
    # failed resync would make the next start believe it is already in sync, so a workbench that
    # was offline during an app update would silently keep a stale checkout for good.
    if [ "$workbench_synced" = yes ] && [ -f "$IMAGE_STAMP" ] && [ -d "$WORKBENCH_DIR/.git" ]; then
        cp -f "$IMAGE_STAMP" "$CHECKOUT_STAMP" || true
    fi
    echo "[entrypoint] checkouts ready"
}

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

# In the background: these clone over the network into the persistent data dir, which openhost
# bind-mounts, and neither is fast or reliable enough to gate startup on. openhost marks an app
# failed if it doesn't answer HTTP within 60s of the container starting (wait_for_ready in
# compute_space/core/apps.py), and nothing the server serves needs these checkouts — the code that
# reads them already handles their absence.
seed_checkouts &

export OPENHOST_DIR
exec python3 -m server.app
