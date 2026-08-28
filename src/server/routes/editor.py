from typing import Any

from litestar import Request
from litestar import Response
from litestar import asgi
from litestar import delete
from litestar import get
from litestar import post
from litestar.params import FromPath
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from server.editor import instances
from server.editor import proxy
from server.editor.install import CODE_SERVER_VERSION
from server.editor.install import is_installed
from server.projects.workspaces import parse_workspace_id
from server.routes.common import JsonDict
from server.routes.common import error
from server.routes.common import json_body


def editor_json(instance: instances.EditorInstance) -> JsonDict:
    return {
        "workspace_id": instance.workspace_id,
        "url": proxy.editor_url(instance.workspace_id),
        "running": True,
    }


@get("/api/editor", sync_to_thread=False)
def list_editors() -> JsonDict:
    """What's running, and whether the editor needs downloading before the first one can start."""
    return {
        "version": CODE_SERVER_VERSION,
        "installed": is_installed(),
        "max_instances": instances.MAX_INSTANCES,
        "instances": [editor_json(i) for i in instances.running_instances()],
    }


@post("/api/editor", status_code=200)
async def start_editor(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Start a workspace's editor and return the URL to load it from.

    Slow on two counts, both only the first time: the very first call downloads code-server, and
    each cold start takes a few seconds. The client shows that rather than blocking a page load.
    """
    data = await json_body(request)
    workspace = parse_workspace_id(str(data.get("workspace_id") or ""))
    if workspace is None:
        return error(400, error="bad_request", message="workspace_id is required")
    if not workspace.path.is_dir():
        return error(404, error="not_found", message=f"no workspace {workspace.id}")

    try:
        instance = await instances.start(workspace)
    except (OSError, RuntimeError, TimeoutError) as e:
        return error(500, error="start_failed", message=f"could not start the editor: {e}")
    return Response(content=editor_json(instance))


@delete("/api/editor/{project_id:str}/{name:str}", status_code=200)
async def stop_editor(project_id: FromPath[str], name: FromPath[str]) -> Response[JsonDict]:
    workspace = parse_workspace_id(f"{project_id}/{name}")
    if workspace is None:
        return error(400, error="bad_request", message="invalid workspace id")
    stopped = await instances.stop(workspace.id)
    return Response(content={"ok": True, "stopped": stopped})


@asgi(proxy.MOUNT_PATH, is_mount=True, copy_scope=True)
async def editor_proxy(scope: Scope, receive: Receive, send: Send) -> None:
    """Serve every running editor under one path on the workbench's own origin.

    A mount rather than a set of routes because the paths belong to VS Code, not to us — and an ASGI
    mount is also the only handler Litestar routes *both* HTTP and WebSocket scopes to, which the
    editor needs: it does all its real work over a WebSocket.
    """
    await proxy.handle(scope, receive, send)
