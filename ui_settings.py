import json

import attr

from config import HOME

# Under $HOME rather than /app: openhost points HOME at the app's persistent data dir, so the
# user's choices survive image rebuilds. Anything written into /app does not.
UI_SETTINGS_PATH = HOME / ".workbench" / "ui.json"

DEFAULT_THEME = "dark"
# Keep in sync with the THEMES map in static/theme.js and the blocks in static/themes.css.
THEMES = (DEFAULT_THEME, "solarized-light", "solarized-dark")


@attr.s(auto_attribs=True, frozen=True)
class UiSettings:
    side_panel: bool = False
    theme: str = DEFAULT_THEME


def load_ui_settings() -> UiSettings:
    """Read the persisted UI settings, falling back to defaults only when nothing is saved yet.

    A malformed file, or a theme this build doesn't know, raises instead of quietly reverting to
    defaults — a setting that silently forgets what you chose is worse to debug than one that says
    so.
    """
    if not UI_SETTINGS_PATH.exists():
        return UiSettings()
    raw = json.loads(UI_SETTINGS_PATH.read_text())
    # .get() so a file written by an older build (before a setting existed) still loads.
    theme = str(raw.get("theme", DEFAULT_THEME))
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r} in {UI_SETTINGS_PATH}; expected one of {', '.join(THEMES)}")
    return UiSettings(side_panel=bool(raw.get("side_panel", False)), theme=theme)


def save_ui_settings(settings: UiSettings) -> None:
    """Persist settings via a temp file + rename, so an interrupted write can't corrupt the file."""
    if settings.theme not in THEMES:
        raise ValueError(f"unknown theme {settings.theme!r}; expected one of {', '.join(THEMES)}")
    UI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = UI_SETTINGS_PATH.with_name(UI_SETTINGS_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps({"side_panel": settings.side_panel, "theme": settings.theme}, indent=2) + "\n")
    tmp_path.replace(UI_SETTINGS_PATH)
