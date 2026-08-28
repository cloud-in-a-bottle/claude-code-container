from pathlib import Path

from server.config import STATE_DIR

# Every editor path lives here, and everything else reaches them as `paths.X` rather than importing
# the names directly: the test suite redirects these at the module to keep itself off the real
# $HOME, and a name copied into another module at import time would not follow.
#
# All of it is under $HOME (which openhost points at the app's persistent data dir) so the install,
# the settings and the installed extensions survive image rebuilds.
VSCODE_DIR = STATE_DIR / "vscode"
# The unpacked code-server release. Fetched on first use rather than baked into the image, which
# would add ~740 MB to it.
INSTALL_DIR = VSCODE_DIR / "install"
# settings.json, keybindings.json and snippets/, symlinked into every instance's user-data-dir.
SHARED_USER_DIR = VSCODE_DIR / "user"
# One extensions dir for all instances: installing an extension once makes it available everywhere.
EXTENSIONS_DIR = VSCODE_DIR / "extensions"
# One user-data-dir per workspace. Sharing a single one across concurrent instances makes them
# collide over workspaceStorage and the `code` CLI's IPC socket, so they each get their own.
INSTANCES_DIR = VSCODE_DIR / "instances"
# code-server writes a default config to ~/.config/code-server unless pointed elsewhere. Ours is
# empty and stays that way; the flags in instances.py are the whole configuration.
CONFIG_PATH = VSCODE_DIR / "config.yaml"
# Deliberately not under $HOME: a socket from a previous run means nothing, and $HOME is long
# enough here that a nested path can overrun the 108-byte AF_UNIX limit.
SOCKETS_DIR = Path("/tmp/workbench-vscode")  # noqa: S108
