# ~/.bashrc for the claude-workbench user.
#
# Don't source /etc/bash.bashrc here: bash auto-loads it for interactive
# non-login shells, and /etc/profile loads it for login shells. Sourcing
# it again from .bashrc caused Ubuntu's sudo motd to print twice.

# Colored prompt + ls/grep aliases. Ubuntu's default /etc/skel/.bashrc sets
# these up, but we ship our own .bashrc that replaces skel's, so we have to
# re-enable them here. server.py exports TERM=xterm-256color for the pty.
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
if [[ $- == *i* ]] && [ ! -f "$HOME/.openhost/compute_space_cli.toml" ]; then
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
