import json

import attr

from server.config import HOME

# Under $HOME rather than /app: openhost points HOME at the app's persistent data dir, so the
# user's choices survive image rebuilds. Anything written into /app does not.
UI_SETTINGS_PATH = HOME / ".workbench" / "ui.json"

DEFAULT_THEME = "solarized-light"
# Keep in sync with the THEMES map in ui/src/themes.js and the blocks in static/themes.css, where
# the default is the bare :root block rather than an attribute selector.
THEMES = ("dark", "solarized-light", "solarized-dark")


@attr.s(auto_attribs=True, frozen=True)
class UiSettings:
    side_panel: bool = False
    theme: str = DEFAULT_THEME
    # What the side panel shows. Server-side, unlike the panel's width, so that Claude can point it
    # at a dev server it just started; None until anyone has, and the panel shows its home page.
    side_panel_url: str | None = None

    def to_json(self) -> dict[str, bool | str | None]:
        return {"side_panel": self.side_panel, "theme": self.theme, "side_panel_url": self.side_panel_url}


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
    url = raw.get("side_panel_url")
    return UiSettings(
        side_panel=bool(raw.get("side_panel", False)), theme=theme, side_panel_url=None if url is None else str(url)
    )


def save_ui_settings(settings: UiSettings) -> None:
    """Persist settings via a temp file + rename, so an interrupted write can't corrupt the file."""
    if settings.theme not in THEMES:
        raise ValueError(f"unknown theme {settings.theme!r}; expected one of {', '.join(THEMES)}")
    UI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = UI_SETTINGS_PATH.with_name(UI_SETTINGS_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(settings.to_json(), indent=2) + "\n")
    tmp_path.replace(UI_SETTINGS_PATH)
