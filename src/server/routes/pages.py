from litestar import MediaType
from litestar import get
from litestar.response import Template

from server.routes.common import NO_CACHE
from server.routes.common import JsonDict
from server.ui_settings import load_ui_settings


@get("/health", sync_to_thread=False)
def health() -> JsonDict:
    return {"status": "ok"}


@get("/", media_type=MediaType.HTML, cache_control=NO_CACHE, sync_to_thread=False)
def index() -> Template:
    settings = load_ui_settings()
    # `theme` is rendered onto <html> so the chrome is already the right colour before the bundle
    # runs; the same values reach the app as window.__WORKBENCH__.
    return Template(
        template_name="index.html",
        context={
            "theme": settings.theme,
            "bootstrap": {"theme": settings.theme, "side_panel": settings.side_panel},
        },
    )
