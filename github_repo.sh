#!/usr/bin/env bash
# Opens a GitHub repo in Claude Code. Inputs via env vars (no shell injection surface):
#   GITHUB_REPO  - clone URL
#   GITHUB_DIR   - absolute destination path
#   CLAUDE_BIN   - full path to the claude binary

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

for _i in 1 2 3; do
    "${CLAUDE_BIN}" --dangerously-skip-permissions && break
    sleep 1
done
exec bash -l
