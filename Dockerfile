# The Solid frontend. vite writes to ../src/server/static/ui, which from /ui is /src/server/static/ui
# in this stage; the runtime image copies that in over the source tree.
FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build


FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        gh glab \
        git tini bash sudo less vim man-db ca-certificates curl wget gnupg \
        htop tree jq ripgrep fd-find fzf tmux ncdu \
        unzip zip file \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Root inside the container is an unprivileged host user (rootless podman, cap-drop=ALL,
# no-new-privileges), so runtime `apt-get install` works without sudo — which
# no_new_privs blocks anyway. IS_SANDBOX tells Claude Code that, so it allows
# --dangerously-skip-permissions as uid 0.
ENV HOME=/root
ENV IS_SANDBOX=1
ENV PATH="/app/.venv/bin:/root/.local/bin:$PATH"

RUN curl -fsSL https://claude.ai/install.sh | bash

ARG OH_VERSION=v0.1.0
RUN uv tool install "oh @ git+https://github.com/imbue-openhost/openhost.git@${OH_VERSION}#subdirectory=compute_space_cli"

# There's no system python: uv fetches the one pinned by requires-python. The venv is on
# PATH, so `python3` is the server's. --no-install-project keeps this layer off the source
# tree, so app edits don't reinstall deps; the project itself is installed below.
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Site rcfile lives under /etc so $HOME — repointed at the persistent data dir at
# runtime — stays untouched and user edits to ~/.bashrc survive image updates.
COPY . /app
COPY --from=ui /src/server/static/ui /app/src/server/static/ui
RUN uv sync --frozen --no-dev \
    && chmod +x /app/*.sh /app/src/server/projects/*.sh \
    && cp /app/workbench.sh /etc/profile.d/workbench.sh \
    && echo '[ -r /etc/profile.d/workbench.sh ] && . /etc/profile.d/workbench.sh' >> /etc/bash.bashrc

# Must stay after every COPY: it then regenerates exactly when image content changed and
# stays put on a pure cache hit. entrypoint.sh diffs it against the copy in
# ~/claude-code-container to tell an app update from a container restart.
RUN date -u +%Y-%m-%dT%H:%M:%SZ > /app/.image-stamp

WORKDIR /root
EXPOSE 5000
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
