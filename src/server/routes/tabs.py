import traceback
from typing import Any

from litestar import Request
from litestar import Response
from litestar import WebSocket
from litestar import delete
from litestar import get
from litestar import post
from litestar import websocket
from litestar.params import FromPath
from litestar.params import FromQuery

from server.projects.workspaces import parse_workspace_id
from server.routes.common import JsonDict
from server.routes.common import error
from server.routes.common import json_body
from server.tabs import _tabs
from server.tabs import handle_terminal_ws
from server.tabs import kick_tab
from server.tabs import kill_tab
from server.tabs import new_tab_in_workspace
from server.tabs import tab_proc_info
from server.tabs import tabs_for_workspace


def tab_json(tab: Any) -> JsonDict:
    program, cwd = tab_proc_info(tab)
    return {
        "id": tab.id,
        "label": tab.label,
        "workspace_id": tab.workspace_id,
        "connected": tab.connected,
        "alive": tab.alive,
        "program": program,
        "cwd": cwd,
    }


@get("/api/tabs", sync_to_thread=False)
def list_tabs(workspace: FromQuery[str] = "") -> list[JsonDict]:
    """The open tabs, restricted to one workspace when `workspace` is given."""
    found = tabs_for_workspace(workspace) if workspace else list(_tabs.values())
    return [tab_json(t) for t in found]


@post("/api/tabs", status_code=200)
async def create_tab(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Open a tab in a workspace. The workspace's first tab runs Claude; the rest are plain bash."""
    data = await json_body(request)
    workspace = parse_workspace_id(str(data.get("workspace_id") or ""))
    if workspace is None:
        return error(400, error="bad_request", message="workspace_id is required")
    if not workspace.path.is_dir():
        return error(404, error="not_found", message=f"no workspace {workspace.id}")

    label = str(data.get("label") or "").strip() or None
    tab = await new_tab_in_workspace(workspace, label=label)
    return Response(content=tab_json(tab))


@post("/api/tabs/{tab_id:str}/kick", status_code=200)
async def kick_tab_client(tab_id: FromPath[str]) -> Response[JsonDict]:
    tab = _tabs.get(tab_id)
    if tab is None:
        return error(404, error="not_found")
    try:
        await kick_tab(tab)
    except TimeoutError:
        return error(504, error="timeout")
    return Response(content={"ok": True})


@delete("/api/tabs/{tab_id:str}", status_code=200)
async def delete_tab(tab_id: FromPath[str]) -> Response[JsonDict]:
    tab = _tabs.pop(tab_id, None)
    if tab is None:
        return error(404, error="not_found")
    kill_tab(tab)
    return Response(content={"ok": True})


@websocket("/terminal/ws")
async def terminal_ws(socket: WebSocket[Any, Any, Any]) -> None:
    try:
        await handle_terminal_ws(socket)
    except Exception:
        traceback.print_exc()
        raise
