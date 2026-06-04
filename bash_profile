# ~/.bash_profile for the claude-workbench user.
#
# New terminal tabs spawn `bash -l`, which reads this file but NOT ~/.bashrc.
# Source .bashrc here so interactive setup (aliases, login banner) runs.

# `bash -l` sources /etc/profile, which resets PATH to the system default
# and drops the additions baked in via Dockerfile `ENV PATH=...`. Re-add
# the workbench paths here so `oh` (~/.local/bin) and the Python venv
# (/opt/venv/bin) are reachable in every new tab.
for d in "$HOME/.local/bin" /opt/venv/bin /usr/sbin /sbin; do
    case ":$PATH:" in
        *":$d:"*) ;;
        *) PATH="$d:$PATH" ;;
    esac
done
export PATH

[ -f ~/.bashrc ] && . ~/.bashrc
