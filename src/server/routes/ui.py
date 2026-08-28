from typing import Any

from litestar import Request
from litestar import Response
from litestar import get
from litestar import post

from server.editor.settings import apply_theme
from server.routes.common import JsonDict
from server.routes.common import error
from server.routes.common import json_body
from server.ui_settings import THEMES
from server.ui_settings import UiSettings
from server.ui_settings import load_ui_settings
from server.ui_settings import save_ui_settings


def _settings_json(settings: UiSettings) -> JsonDict:
    return {"side_panel": settings.side_panel, "theme": settings.theme}


@get("/api/ui/settings", sync_to_thread=False)
def get_ui_settings() -> JsonDict:
    return _settings_json(load_ui_settings())


@post("/api/ui/settings", status_code=200)
async def update_ui_settings(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Update UI settings. Keys left out keep their current value.

    `side_panel` takes effect on the next page load; `theme` is applied live by the client.
    """
    data = await json_body(request)
    known = {"side_panel", "theme"}
    if not known & data.keys():
        return error(400, error=f"expected at least one of: {', '.join(sorted(known))}")

    current = load_ui_settings()
    theme = str(data.get("theme", current.theme))
    if theme not in THEMES:
        return error(400, error=f"unknown theme {theme!r}; expected one of: {', '.join(THEMES)}")

    settings = UiSettings(side_panel=bool(data.get("side_panel", current.side_panel)), theme=theme)
    save_ui_settings(settings)
    # The editor reads its theme from the shared settings file, which running instances watch, so
    # this recolours any open editor panel without a reload.
    apply_theme(settings.theme)
    return Response(content=_settings_json(settings))
