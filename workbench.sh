# Site config for the claude-workbench. Installed at /etc/profile.d/workbench.sh
# so login shells (`bash -l`) pick it up via /etc/profile, and sourced from the
# bottom of /etc/bash.bashrc (a one-line `source` is appended in the Dockerfile)
# so non-login interactive bash gets it too. The guard below makes the
# double-include safe: Ubuntu's /etc/profile sources /etc/bash.bashrc before
# /etc/profile.d, so on a login shell this file is reached twice.
#
# Living here instead of $HOME/.bashrc means image updates flow through cleanly
# without clobbering anything the user wrote in their own dotfiles.

if [ -n "${_WORKBENCH_RC_LOADED:-}" ]; then
    return
fi
_WORKBENCH_RC_LOADED=1

# `bash -l` sources /etc/profile, which resets PATH to the system default and
# drops the additions baked in via Dockerfile `ENV PATH=...`. Re-add the
# workbench paths so `oh` (~/.local/bin) and the Python venv (/opt/venv/bin)
# are reachable in every new tab.
for d in "$HOME/.local/bin" /opt/venv/bin /usr/sbin /sbin; do
    case ":$PATH:" in
        *":$d:"*) ;;
        *) PATH="$d:$PATH" ;;
    esac
done
export PATH

# Everything below is interactive-only.
case $- in *i*) ;; *) return;; esac

# Colored prompt + ls/grep aliases. server.py exports TERM=xterm-256color
# for the pty.
case "$TERM" in
    xterm-color|*-256color) color_prompt=yes;;
esac

if [ "$color_prompt" = yes ]; then
    PS1='\[\033[01;31m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '
else
    PS1='\u@\h:\w\$ '
fi
unset color_prompt

if [ -x /usr/bin/dircolors ]; then
    eval "$(dircolors -b)"
    alias ls='ls --color=auto'
    alias grep='grep --color=auto'
    alias fgrep='fgrep --color=auto'
    alias egrep='egrep --color=auto'
fi

alias ll='ls -alF'
alias la='ls -A'
alias l='ls -CF'

alias claude='claude --dangerously-skip-permissions'

# Interactive login banner: prompt to configure the `oh` CLI if no config
# exists yet. The workbench tries to seed this file on startup using the
# OPENHOST_ZONE_DOMAIN env var for the hostname and the OH_TOKEN secret from
# secrets-v2; if the token isn't set, fall back to nudging the user.
if [ ! -f "$HOME/.openhost/compute_space_cli.toml" ]; then
    cat <<'EOF'
────────────────────────────────────────────────────────────────
  The `oh` openhost CLI is installed but not configured.

  To auto-configure on next start, set this secret in the
  secrets app, then restart this workbench:

      OH_TOKEN   an API token for this compute space

  Or configure it interactively now:

      oh instance login
────────────────────────────────────────────────────────────────
EOF
fi
