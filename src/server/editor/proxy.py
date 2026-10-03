from typing import cast

import attr
import websockets
from litestar.enums import ScopeType
from litestar.types import HTTPScope
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from litestar.types import WebSocketAcceptEvent
from litestar.types import WebSocketScope
from websockets.asyncio.client import unix_connect

from server import asgi_proxy
from server.editor import instances
from server.editor.instances import EditorInstance
from server.projects.workspaces import Workspace
from server.projects.workspaces import parse_workspace_id

MOUNT_PATH = "/vscode"


@attr.s(auto_attribs=True, frozen=True)
class Target:
    """Which workspace a request under the mount is for, and what's left of the path for upstream."""

    workspace: Workspace
    # Always starts with "/". Percent-encoding is passed through exactly as the browser sent it.
    upstream_path: str


def editor_url(workspace_id: str) -> str:
    return f"{MOUNT_PATH}/{workspace_id}/"


def parse_target(raw_path: str) -> Target | None:
    """Split `/vscode/<project>/<workspace>/<rest>` into the workspace and the upstream path.

    Takes the raw path (see `asgi_proxy.raw_path_of`): the mount-rewritten one would 404 every asset
    the editor loads.
    """
    if not raw_path.startswith(MOUNT_PATH + "/"):
        return None
    rest = raw_path[len(MOUNT_PATH) + 1 :]
    project_id, _, tail = rest.partition("/")
    name, slash, remainder = tail.partition("/")
    workspace = parse_workspace_id(f"{project_id}/{name}")
    if workspace is None:
        return None
    return Target(workspace=workspace, upstream_path="/" + remainder if slash else "/")


def target_of(scope: Scope) -> Target | None:
    return parse_target(asgi_proxy.raw_path_of(scope))


async def _forward_http(
    instance: EditorInstance, target: Target, scope: HTTPScope, receive: Receive, send: Send
) -> None:
    query = scope["query_string"].decode()
    url = "http://editor" + target.upstream_path + (f"?{query}" if query else "")
    client = instances.http_client(instance)
    await asgi_proxy.forward_http(client, url, asgi_proxy.request_headers(scope), scope, receive, send)


async def _forward_ws(
    instance: EditorInstance, target: Target, scope: WebSocketScope, receive: Receive, send: Send
) -> None:
    query = scope["query_string"].decode()
    url = "ws://editor" + target.upstream_path + (f"?{query}" if query else "")
    await receive()  # websocket.connect

    try:
        upstream = await unix_connect(str(instance.socket_path), url, max_size=None, open_timeout=30)
    except (OSError, websockets.InvalidHandshake, TimeoutError) as e:
        print(f"[editor] websocket to {target.workspace.id} failed: {e!r}", flush=True)
        await asgi_proxy.close_websocket(send, 1011)
        return

    accept: WebSocketAcceptEvent = {"type": "websocket.accept", "subprotocol": None, "headers": []}
    await send(accept)
    await asgi_proxy.pump_websocket(upstream, receive, send)


async def handle(scope: Scope, receive: Receive, send: Send) -> None:
    """Serve one request under `/vscode/<project>/<workspace>/` from that workspace's editor."""
    is_websocket = scope["type"] == ScopeType.WEBSOCKET
    target = target_of(scope)

    if target is None:
        if is_websocket:
            await receive()
            await asgi_proxy.close_websocket(send, 1008)
            return
        await asgi_proxy.send_json(send, 404, {"error": "not_found", "message": "not an editor path"})
        return

    instance = instances.running(target.workspace.id)
    if instance is None:
        # A cold start takes seconds, so it belongs behind an explicit POST /api/editor where the
        # client can show progress, rather than inside a page load that would just look hung.
        if is_websocket:
            await receive()
            await asgi_proxy.close_websocket(send, 1011)
            return
        message = f"no editor running for {target.workspace.id}"
        await asgi_proxy.send_json(send, 503, {"error": "not_running", "message": message})
        return

    instances.touch(target.workspace.id)
    if is_websocket:
        await _forward_ws(instance, target, cast("WebSocketScope", scope), receive, send)
    else:
        await _forward_http(instance, target, cast("HTTPScope", scope), receive, send)
