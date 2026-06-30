FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        git ca-certificates curl wget tini bash less vim sudo \
        htop tree jq ripgrep fd-find fzf tmux ncdu \
        unzip zip file man-db gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 from NodeSource — Ubuntu's own nodejs package lags badly.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI — install from the official apt repo so it stays current.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# GitLab CLI — official binary release (no apt repo available).
ARG GLAB_VERSION=1.65.0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64|arm64) ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_${arch}.tar.gz" \
        -o /tmp/glab.tar.gz; \
    tar -xzf /tmp/glab.tar.gz -C /usr/local bin/glab; \
    rm /tmp/glab.tar.gz

RUN npm install -g @anthropic-ai/claude-code

# Python deps for the server.
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir 'quart>=0.19' 'hypercorn>=0.16' 'httpx>=0.27' 'tomli-w>=1.0'
ENV PATH="/opt/venv/bin:$PATH"

# uv — used to install the `oh` openhost CLI. Ubuntu 24.04 ships Python 3.12
# natively, but uv still manages the tool's isolated environment cleanly.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Run as root inside the container. openhost launches workbench containers
# under rootless podman with --cap-drop=ALL and --security-opt=no-new-privileges,
# so "root" inside is still mapped to an unprivileged host user and can't escape
# the container — but it lets `apt-get install` and friends work at runtime
# without sudo (which no_new_privs blocks anyway).
ENV HOME=/root

# Claude Code refuses --dangerously-skip-permissions when uid == 0 unless it
# believes it's already sandboxed. We are (rootless podman, no_new_privs,
# cap-drop=ALL), so tell it so.
ENV IS_SANDBOX=1

WORKDIR /app
COPY server.py /app/server.py
COPY open_workspace.sh /app/open_workspace.sh
COPY templates /app/templates
COPY static /app/static
COPY skills /app/skills
COPY entrypoint.sh /app/entrypoint.sh
# Site rcfile: lives under /etc so $HOME (which we point at the persistent data
# dir at runtime) stays untouched and user edits to ~/.bashrc/~/.bash_profile
# survive image updates.
COPY workbench.sh /etc/profile.d/workbench.sh
RUN echo '[ -r /etc/profile.d/workbench.sh ] && . /etc/profile.d/workbench.sh' \
        >> /etc/bash.bashrc \
    && chmod +x /app/entrypoint.sh

WORKDIR /root

# Install the `oh` openhost CLI. uv fetches Python 3.12 automatically
# (the CLI requires it).
ENV PATH="/root/.local/bin:$PATH"
RUN uv tool install "oh @ git+https://github.com/imbue-openhost/openhost.git@v0.1.0#subdirectory=compute_space_cli"

EXPOSE 5000
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
