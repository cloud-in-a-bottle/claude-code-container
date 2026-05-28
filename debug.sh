#!/usr/bin/env bash
# Runs in a /debug terminal. All inputs arrive via env vars (DEBUG_REPO,
# DEBUG_DIR, DEBUG_REF, DEBUG_PROMPT) set by server.py — never string
# interpolation — so there's nothing to escape and no shell injection surface.
#
# We deliberately don't `set -e`: if a step fails we report it and still drop
# the user into a shell so they can poke around.
#
# If the target dir already holds a checkout (e.g. the link was clicked before),
# we reuse it rather than clobbering it: fetch, then — if the tree is dirty —
# interactively ask what to do with the local changes before checking out the
# requested ref. The prompt loop is guarded against EOF so a closed/stale tab
# bails to a shell instead of spinning, and it re-checks the tree after each
# answer so it never acts on a snapshot that went stale while we waited.

echo
if [ -d "$DEBUG_DIR/.git" ]; then
    echo "[workbench] reusing existing checkout at $DEBUG_DIR"
    cd "$DEBUG_DIR" || exec bash -l
    echo "[workbench] fetching latest"
    git fetch --all --tags --prune || echo "[workbench] fetch failed; continuing with what's on disk." >&2
    while :; do
        before="$(git status --porcelain)"
        [ -n "$before" ] || break
        echo
        echo "[workbench] this checkout has uncommitted changes:"
        git status --short
        printf '  [c]ommit to a wip branch / [s]tash / [d]rop / [k]eep as-is and stop here? '
        read -r ans || { echo; echo "[workbench] no input (stale tab?); leaving changes untouched."; exec bash -l; }
        # The tree could have changed while we waited for an answer (another tab
        # on the same container). Don't act on a stale snapshot — re-prompt.
        if [ "$before" != "$(git status --porcelain)" ]; then
            echo "[workbench] working tree changed while you were deciding; re-checking."
            continue
        fi
        case "$ans" in
            c|C)
                git checkout -b "workbench-wip-$(date +%Y%m%d-%H%M%S)" \
                    && git add -A \
                    && git commit -qm "workbench autosave" \
                    && echo "[workbench] committed to $(git rev-parse --abbrev-ref HEAD)"
                ;;
            s|S)
                git stash push -u -m "workbench autosave $(date +%Y%m%d-%H%M%S)" \
                    && echo "[workbench] stashed; recover later with: git stash pop"
                ;;
            d|D)
                git reset --hard && git clean -fd && echo "[workbench] dropped local changes"
                ;;
            k|K)
                echo "[workbench] keeping changes; not checking out $DEBUG_REF."
                exec bash -l
                ;;
            *)
                echo "[workbench] please answer c, s, d, or k."
                ;;
        esac
    done
else
    echo "[workbench] cloning $DEBUG_REPO"
    if ! git clone -- "$DEBUG_REPO" "$DEBUG_DIR"; then
        echo "[workbench] clone failed; dropping you into a shell." >&2
        exec bash -l
    fi
    cd "$DEBUG_DIR" || exec bash -l
fi
if [ -n "$DEBUG_REF" ]; then
    echo "[workbench] checking out $DEBUG_REF"
    git checkout "$DEBUG_REF" || echo "[workbench] checkout of $DEBUG_REF failed." >&2
fi
if [ -n "$DEBUG_PROMPT" ]; then
    exec claude --dangerously-skip-permissions "$DEBUG_PROMPT"
fi
exec bash -l
