FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        gh glab \
        git tini bash sudo less vim man-db ca-certificates curl wget gnupg \
        nodejs npm \
        htop tree jq ripgrep fd-find fzf tmux ncdu \
        unzip zip file \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Root inside the container is an unprivileged host user (rootless podman, cap-drop=ALL,
# no-new-privileges), so runtime `apt-get install` works without sudo — which
# no_new_privs blocks anyway. IS_SANDBOX tells Claude Code that, so it allows
# --dangerously-skip-permissions as uid 0.
#
# CLAUDE_CODE_SANDBOXED is the second half: it marks every directory as trusted, so the "Is this a
# project you trust?" dialog never appears. Trust is recorded per absolute path in ~/.claude.json
# and a trusted parent doesn't cover a child that is its own git repo, so without this every new
# workspace -- each a fresh path and a fresh clone -- opens with that prompt. Every repo here was
# cloned because the user asked the workbench for it, and --dangerously-skip-permissions is already
# on, so there is nothing for the dialog to protect.
ENV HOME=/root
ENV IS_SANDBOX=1
ENV CLAUDE_CODE_SANDBOXED=1
ENV PATH="/app/.venv/bin:/root/.local/bin:$PATH"

RUN curl -fsSL https://claude.ai/install.sh | bash

ARG OH_VERSION=v0.1.0
RUN uv tool install "oh @ git+https://github.com/imbue-openhost/openhost.git@${OH_VERSION}#subdirectory=compute_space_cli"

# Everything from here down is ordered by how often it changes, so editing the app only rebuilds
# the last few layers. There's no system python: uv fetches the one pinned by requires-python. The
# venv is on PATH, so `python3` is the server's. --no-install-project keeps this layer off the
# source tree, so app edits don't reinstall dependencies.
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The frontend's dependencies, on their own layer for the same reason: the manifests come over
# without the source, so editing ui/ rebuilds the bundle without reinstalling. They stay in the
# image rather than being discarded with a build stage, so the bundle can be rebuilt in place.
COPY ui/package.json ui/package-lock.json ./ui/
RUN cd ui && npm ci && npm cache clean --force

# Site rcfile lives under /etc so $HOME — repointed at the persistent data dir at
# runtime — stays untouched and user edits to ~/.bashrc survive image updates.
COPY workbench.sh ./
RUN cp /app/workbench.sh /etc/profile.d/workbench.sh \
    && echo '[ -r /etc/profile.d/workbench.sh ] && . /etc/profile.d/workbench.sh' >> /etc/bash.bashrc

# The parts of the repo that rarely move: the entrypoint, the manifest, the docs the workbench
# points Claude at, the bundled skills, and the global instructions the entrypoint symlinks to
# ~/.claude/CLAUDE.md.
COPY entrypoint.sh openhost.toml justfile README.md claude.md style_guide.md ./
COPY .dockerignore .gitignore .pre-commit-config.yaml Dockerfile ./
COPY services/ ./services/
COPY skills/ ./skills/
COPY claude-home/ ./claude-home/
RUN chmod +x /app/entrypoint.sh

# The app. `uv sync` installs the project editable, so it points at /app/src rather than copying
# it, and the server serves its templates and static files from there.
COPY src/ ./src/
RUN uv sync --frozen --no-dev \
    && chmod +x /app/src/server/projects/*.sh

# The Solid frontend. .dockerignore drops **/node_modules, so this copy lands beside the
# dependencies installed above rather than over them. vite's outDir is ../src/server/static/ui,
# which from /app/ui is the static dir the server already serves.
COPY ui/ ./ui/
RUN cd ui && npm run build

# Not used at runtime; carried so the image is a complete copy of what built it.
COPY tests/ ./tests/

# Must stay after every COPY: it then regenerates exactly when image content changed and
# stays put on a pure cache hit. entrypoint.sh diffs it against the copy in
# ~/claude-code-container to tell an app update from a container restart.
RUN date -u +%Y-%m-%dT%H:%M:%SZ > /app/.image-stamp

WORKDIR /root
EXPOSE 5000
# SIGHUP is ignored before tini is exec'd, and SIG_IGN survives exec, so pid 1 inherits it.
# This tini installs no SIGHUP handler of its own (its SigCgt is 0), so without this a `kill -HUP 1`
# from anything inside the container kills pid 1 by default action and takes every terminal in
# every workspace with it -- and this container runs Claude, the user's shells and its own test
# suite by design. Tab processes put the default back before exec (see reset_child_signals), so
# terminals can still be hung up normally.
ENTRYPOINT ["/bin/sh", "-c", "trap '' HUP; exec /usr/bin/tini -- /app/entrypoint.sh"]
