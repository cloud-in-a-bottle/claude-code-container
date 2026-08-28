import asyncio
from typing import Any
from typing import cast

import hypercorn.asyncio
import hypercorn.config
from litestar import Litestar
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig

from server.config import APP_DIR
from server.config import PORT
from server.projects.seed import seed_projects
from server.remote_services import seed_gh_auth
from server.remote_services import seed_oh_config
from server.routes.common import NO_CACHE
from server.routes.open_workspace import open_workspace
from server.routes.pages import health
from server.routes.pages import index
from server.routes.projects import create_project
from server.routes.projects import create_workspace
from server.routes.projects import delete_project
from server.routes.projects import list_projects
from server.routes.projects import remove_workspace
from server.routes.projects import update_project
from server.routes.tabs import create_tab
from server.routes.tabs import delete_tab
from server.routes.tabs import kick_tab_client
from server.routes.tabs import list_tabs
from server.routes.tabs import terminal_ws
from server.routes.ui import get_ui_settings
from server.routes.ui import update_ui_settings
from server.tabs import persist_tabs_periodically
from server.tabs import restore_tabs


async def _on_startup() -> None:
    await seed_oh_config()
    await seed_gh_auth()
    seed_projects()
    # Bring back the tabs from the previous run before any client connects, so the first page
    # load already shows them instead of racing to create a fresh one.
    await restore_tabs()
    asyncio.create_task(persist_tabs_periodically())  # noqa: RUF006


app = Litestar(
    route_handlers=[
        health,
        index,
        get_ui_settings,
        update_ui_settings,
        list_projects,
        create_project,
        update_project,
        delete_project,
        create_workspace,
        remove_workspace,
        list_tabs,
        create_tab,
        kick_tab_client,
        delete_tab,
        open_workspace,
        terminal_ws,
        create_static_files_router(path="/static", directories=[APP_DIR / "static"], cache_control=NO_CACHE),
    ],
    template_config=TemplateConfig(directory=APP_DIR / "templates", engine=JinjaTemplateEngine),
    on_startup=[_on_startup],
)


def main() -> None:
    cfg = hypercorn.config.Config()
    cfg.bind = [f"0.0.0.0:{PORT}"]
    cfg.accesslog = "-"
    # hypercorn describes ASGI apps with its own Scope TypedDicts; Litestar's are structurally
    # equivalent but nominally distinct, so the two annotations can't be reconciled.
    asyncio.run(hypercorn.asyncio.serve(cast("Any", app), cfg))


if __name__ == "__main__":
    main()
