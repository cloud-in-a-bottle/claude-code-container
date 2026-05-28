#!/usr/bin/env bash
# Runs in an /open-workspace terminal. All inputs arrive via env vars
# (WORKSPACE_REPO, WORKSPACE_DIR, WORKSPACE_REF, and optionally
# WORKSPACE_GITHUB_TOKEN) set by server.py — never string interpolation — so
# there's nothing to escape and no shell injection surface.
#
# We deliberately don't `set -e`: if a step fails we report it and still drop
# the user into a shell so they can poke around (the provider may degrade
# gracefully — see REQ-IMPL-7 in the open-workspace contract).
#
# If WORKSPACE_GITHUB_TOKEN is set, the repo is private and openhost minted us a
# transient token. We inject it into the clone/fetch URL but always leave the
# persisted `origin` URL token-free, matching how openhost clones private repos
# (compute_space/core/apps.py): the token never lands on disk.
#
# If the target dir already holds a checkout (e.g. the link was clicked before),
# we reuse it rather than clobbering it: fetch, then — if the tree is dirty —
# interactively ask what to do with the local changes before checking out the
# requested ref. The prompt loop is guarded against EOF so a closed/stale tab
# bails to a shell instead of spinning, and it re-checks the tree after each
# answer so it never acts on a snapshot that went stale while we waited.

CLEAN_URL="$WORKSPACE_REPO"
# Build a token-authenticated URL for http(s) repos when we have a token; for
# ssh/git@ transports the token can't be applied, so it stays the clean URL.
AUTHED_URL="$CLEAN_URL"
if [ -n "${WORKSPACE_GITHUB_TOKEN:-}" ]; then
    case "$CLEAN_URL" in
        https://*) AUTHED_URL="https://${WORKSPACE_GITHUB_TOKEN}@${CLEAN_URL#https://}" ;;
        http://*)  AUTHED_URL="http://${WORKSPACE_GITHUB_TOKEN}@${CLEAN_URL#http://}" ;;
    esac
fi
# AUTHED_URL is a plain (non-exported) shell var; once we've captured it the
# token env var is no longer needed, so drop it before any `exec bash` so it
# can't leak into the interactive shell the user lands in.
unset WORKSPACE_GITHUB_TOKEN

echo
if [ -d "$WORKSPACE_DIR/.git" ]; then
    echo "[workbench] reusing existing checkout at $WORKSPACE_DIR"
    cd "$WORKSPACE_DIR" || exec bash -l
    echo "[workbench] fetching latest"
    if [ "$AUTHED_URL" != "$CLEAN_URL" ]; then
        # Authenticate origin just for the fetch, then restore the existing
        # (token-free) URL so the token isn't persisted on disk.
        orig_url="$(git remote get-url origin 2>/dev/null)"
        git remote set-url origin "$AUTHED_URL"
        git fetch --all --tags --prune || echo "[workbench] fetch failed; continuing with what's on disk." >&2
        [ -n "$orig_url" ] && git remote set-url origin "$orig_url"
    else
        git fetch --all --tags --prune || echo "[workbench] fetch failed; continuing with what's on disk." >&2
    fi
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
                echo "[workbench] keeping changes; not checking out $WORKSPACE_REF."
                exec bash -l
                ;;
            *)
                echo "[workbench] please answer c, s, d, or k."
                ;;
        esac
    done
else
    echo "[workbench] cloning $CLEAN_URL"
    if ! git clone -- "$AUTHED_URL" "$WORKSPACE_DIR"; then
        echo "[workbench] clone failed; dropping you into a shell." >&2
        exec bash -l
    fi
    cd "$WORKSPACE_DIR" || exec bash -l
    # `git clone` records the URL we cloned with as origin; if that carried a
    # token, replace it with the clean URL so the token isn't persisted.
    if [ "$AUTHED_URL" != "$CLEAN_URL" ]; then
        git remote set-url origin "$CLEAN_URL"
    fi
fi
if [ -n "$WORKSPACE_REF" ]; then
    echo "[workbench] checking out $WORKSPACE_REF"
    git checkout "$WORKSPACE_REF" \
        || echo "[workbench] checkout of $WORKSPACE_REF failed; staying on the default branch." >&2
fi
exec bash -l
