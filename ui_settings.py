import json

import attr

from config import HOME

# Under $HOME rather than /app: openhost points HOME at the app's persistent data dir, so the
# user's choice survives image rebuilds. Anything written into /app does not.
UI_SETTINGS_PATH = HOME / ".workbench" / "ui.json"


@attr.s(auto_attribs=True, frozen=True)
class UiSettings:
    side_panel: bool = False


def load_ui_settings() -> UiSettings:
    """Read the persisted UI settings, falling back to defaults only when nothing is saved yet.

    A malformed file raises instead of quietly reverting to defaults — a toggle that silently
    forgets what you set is worse to debug than one that says so.
    """
    if not UI_SETTINGS_PATH.exists():
        return UiSettings()
    raw = json.loads(UI_SETTINGS_PATH.read_text())
    # .get() so a file written by an older build (before a future setting existed) still loads.
    return UiSettings(side_panel=bool(raw.get("side_panel", False)))


def save_ui_settings(settings: UiSettings) -> None:
    """Persist settings via a temp file + rename, so an interrupted write can't corrupt the file."""
    UI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = UI_SETTINGS_PATH.with_name(UI_SETTINGS_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps({"side_panel": settings.side_panel}, indent=2) + "\n")
    tmp_path.replace(UI_SETTINGS_PATH)
