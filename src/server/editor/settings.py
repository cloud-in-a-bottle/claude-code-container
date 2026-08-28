import json
import shutil
from pathlib import Path

from server.editor import paths
from server.ui_settings import DEFAULT_THEME

# What each instance shares with every other one. Anything not in here is per-workspace.
SHARED_ENTRIES = ("settings.json", "keybindings.json", "snippets")

# The workbench's colour schemes mapped onto VS Code's own built-in themes, so the editor follows
# the picker in the tab bar. Keys must cover ui_settings.THEMES; a test asserts they do.
THEME_NAMES = {
    "dark": "Dark Modern",
    "solarized-light": "Solarized Light",
    "solarized-dark": "Solarized Dark",
}

# Seeded once, on first use, and then owned by the user. Everything here is either a resource
# decision this container has to make for them, or a default that's wrong in a workbench.
DEFAULT_SETTINGS = {
    # The bundled Copilot spawns a ~200 MB language server per instance, signed out and unasked.
    # With two instances live in a 4 GB container that is the single largest thing to switch off.
    "chat.disableAIFeatures": True,
    "workbench.colorTheme": THEME_NAMES[DEFAULT_THEME],
    "workbench.startupEditor": "none",
    # With chat off the secondary sidebar has nothing left to hold, and it opens by default as an
    # empty strip down the side of the panel.
    "workbench.secondarySideBar.defaultVisibility": "hidden",
    "telemetry.telemetryLevel": "off",
    "update.mode": "none",
    "extensions.autoCheckUpdates": False,
    "extensions.autoUpdate": False,
    # Every workspace here is a repo the user asked the workbench to clone, so the trust prompt has
    # nothing to add — and unanswered it leaves the editor in Restricted Mode.
    "security.workspace.trust.enabled": False,
    "git.openRepositoryInParentFolders": "never",
    # inotify watches are a host-wide budget that a container cannot raise, and every instance
    # spends from it. These are the directories that cost the most and are worth the least.
    "files.watcherExclude": {
        "**/.git/objects/**": True,
        "**/.git/subtree-cache/**": True,
        "**/node_modules/**": True,
        "**/.venv/**": True,
        "**/venv/**": True,
        "**/target/**": True,
        "**/dist/**": True,
        "**/build/**": True,
        "**/__pycache__/**": True,
        "**/.mypy_cache/**": True,
        "**/.pytest_cache/**": True,
        "**/.ruff_cache/**": True,
    },
}


def shared_settings_path() -> Path:
    return paths.SHARED_USER_DIR / "settings.json"


def ensure_shared_config() -> None:
    """Create the shared config on first use. Files that already exist are left exactly as they are."""
    paths.SHARED_USER_DIR.mkdir(parents=True, exist_ok=True)
    paths.EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (paths.SHARED_USER_DIR / "snippets").mkdir(exist_ok=True)

    settings = shared_settings_path()
    if not settings.exists():
        settings.write_text(json.dumps(DEFAULT_SETTINGS, indent=2) + "\n")
    keybindings = paths.SHARED_USER_DIR / "keybindings.json"
    if not keybindings.exists():
        keybindings.write_text("[]\n")


def link_shared_config(user_data_dir: Path) -> None:
    """Point one instance's User/ at the shared settings, keybindings and snippets.

    Symlinks rather than copies, and the direction matters: VS Code resolves the link before its
    atomic write, so editing settings from inside the editor lands in the shared file and reaches
    every other instance, instead of replacing the link with a private copy.
    """
    ensure_shared_config()
    user_dir = user_data_dir / "User"
    user_dir.mkdir(parents=True, exist_ok=True)

    for name in SHARED_ENTRIES:
        link = user_dir / name
        target = paths.SHARED_USER_DIR / name
        if link.is_symlink():
            if link.readlink() == target:
                continue
            link.unlink()
        elif link.is_dir():
            # A real directory from an install that predates the sharing, or one VS Code made.
            shutil.rmtree(link)
        elif link.exists():
            link.unlink()
        link.symlink_to(target)


def apply_theme(theme: str) -> None:
    """Point the editor at the VS Code theme matching the workbench's colour scheme.

    Best-effort on purpose, and the only place in the editor code that is. This runs as a side
    effect of saving a UI setting the workbench has already applied, the file belongs to the user
    (who may well have put comments in it, which VS Code allows and `json` does not), and a
    mismatched editor theme is not worth either a failed request or overwriting their settings.
    """
    name = THEME_NAMES.get(theme)
    if name is None:
        return
    settings = shared_settings_path()
    if not settings.exists():
        return
    try:
        current = json.loads(settings.read_text())
    except (OSError, ValueError) as e:
        print(f"[editor] leaving {settings} alone; could not read it as JSON: {e}", flush=True)
        return
    if not isinstance(current, dict) or current.get("workbench.colorTheme") == name:
        return
    current["workbench.colorTheme"] = name
    tmp_path = settings.with_name(settings.name + ".tmp")
    tmp_path.write_text(json.dumps(current, indent=2) + "\n")
    tmp_path.replace(settings)
