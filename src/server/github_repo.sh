#!/usr/bin/env bash
# Opens a GitHub repo in Claude Code. Inputs via env vars (no shell injection surface):
#   GITHUB_REPO  - clone URL
#   GITHUB_DIR   - absolute destination path
#   CLAUDE_BIN   - full path to the claude binary
#   CLAUDE_SESSION_ID - UUID pinning this tab's conversation, so a restart can reattach to it

echo
if [ -d "${GITHUB_DIR}/.git" ]; then
    echo "[workbench] using existing checkout at ${GITHUB_DIR}"
    cd "${GITHUB_DIR}"
else
    echo "[workbench] cloning ${GITHUB_REPO} ..."
    if ! git clone -- "${GITHUB_REPO}" "${GITHUB_DIR}"; then
        echo "[workbench] clone failed; dropping to shell." >&2
        exec bash -l
    fi
    cd "${GITHUB_DIR}"
fi

# Seed the openhost-context skill into CLAUDE.md so Claude Code picks it up
# automatically on startup. Only created if the repo doesn't have its own
# CLAUDE.md; kept local via .git/info/exclude so it never appears in git status.
if [ ! -f CLAUDE.md ]; then
    cp /app/skills/openhost/SKILL.md CLAUDE.md
    echo "CLAUDE.md" >> .git/info/exclude 2>/dev/null || true
fi

# Create the pinned session, or rejoin it if a crashed earlier attempt already made it —
# claude rejects --session-id for an id that exists.
for _i in 1 2 3; do
    { "${CLAUDE_BIN}" --session-id "${CLAUDE_SESSION_ID}" --dangerously-skip-permissions ||
        "${CLAUDE_BIN}" --resume "${CLAUDE_SESSION_ID}" --dangerously-skip-permissions; } && break
    sleep 1
done
exec bash -l
