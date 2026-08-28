import asyncio
import json
from typing import cast

import attr
import websockets
from litestar.enums import ScopeType
from litestar.types import HTTPRequestEvent
from litestar.types import HTTPResponseBodyEvent
from litestar.types import HTTPResponseStartEvent
from litestar.types import HTTPScope
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from litestar.types import WebSocketAcceptEvent
from litestar.types import WebSocketCloseEvent
from litestar.types import WebSocketScope
from litestar.types import WebSocketSendEvent
from websockets.asyncio.client import unix_connect

from server.editor import instances
from server.editor.instances import EditorInstance
from server.projects.workspaces import Workspace
from server.projects.workspaces import parse_workspace_id

MOUNT_PATH = "/vscode"

# Connection-level headers belong to the hop that sent them; forwarding them corrupts the next one.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailers",
        "upgrade",
        "proxy-authorization",
        "proxy-authenticate",
    }
)


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

    Taken from the *raw* path rather than the ASGI `path`, because Litestar's mount rewrites that
    one and appends a trailing slash to it — which would turn `/static/out/main.js` into
    `/static/out/main.js/` and 404 every asset the editor loads.
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
    raw_path = scope.get("raw_path") or scope["path"].encode()
    return parse_target(bytes(raw_path).decode("utf-8", "replace"))


async def _send_json(send: Send, status: int, body: dict[str, str]) -> None:
    start: HTTPResponseStartEvent = {
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")],
    }
    await send(start)
    payload: HTTPResponseBodyEvent = {
        "type": "http.response.body",
        "body": json.dumps(body).encode(),
        "more_body": False,
    }
    await send(payload)


async def _close_websocket(send: Send, code: int) -> None:
    close: WebSocketCloseEvent = {"type": "websocket.close", "code": code, "reason": ""}
    await send(close)


async def _request_body(receive: Receive) -> bytes:
    body = bytearray()
    more_body = True
    while more_body:
        message = cast("HTTPRequestEvent", await receive())
        body.extend(message.get("body", b""))
        more_body = bool(message.get("more_body", False))
    return bytes(body)


async def _forward_http(
    instance: EditorInstance, target: Target, scope: HTTPScope, receive: Receive, send: Send
) -> None:
    query = scope["query_string"].decode()
    url = "http://editor" + target.upstream_path + (f"?{query}" if query else "")
    headers = [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in scope["headers"]
        if key.decode("latin-1").lower() not in HOP_BY_HOP
    ]

    client = instances.http_client(instance)
    request = client.build_request(scope["method"], url, headers=headers, content=await _request_body(receive))
    response = await client.send(request, stream=True)
    try:
        start: HTTPResponseStartEvent = {
            "type": "http.response.start",
            "status": response.status_code,
            "headers": [
                (key.encode("latin-1"), value.encode("latin-1"))
                for key, value in response.headers.multi_items()
                if key.lower() not in HOP_BY_HOP
            ],
        }
        await send(start)
        # Raw, undecoded bytes: the response keeps its own content-encoding header, so decoding it
        # here while forwarding that header would leave the browser unable to read it.
        async for chunk in response.aiter_raw():
            body: HTTPResponseBodyEvent = {"type": "http.response.body", "body": chunk, "more_body": True}
            await send(body)
        end: HTTPResponseBodyEvent = {"type": "http.response.body", "body": b"", "more_body": False}
        await send(end)
    finally:
        await response.aclose()


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
        await _close_websocket(send, 1011)
        return

    accept: WebSocketAcceptEvent = {"type": "websocket.accept", "subprotocol": None, "headers": []}
    await send(accept)

    async def browser_to_editor() -> None:
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            frame = cast("dict[str, bytes | str | None]", message)
            data = frame.get("bytes")
            await upstream.send(data if data is not None else str(frame.get("text") or ""))

    async def editor_to_browser() -> None:
        async for message in upstream:
            frame: WebSocketSendEvent = (
                {"type": "websocket.send", "bytes": message, "text": None}
                if isinstance(message, bytes)
                else {"type": "websocket.send", "bytes": None, "text": message}
            )
            await send(frame)

    pumps = [asyncio.create_task(browser_to_editor()), asyncio.create_task(editor_to_browser())]
    try:
        # Either direction closing ends the connection; the editor reconnects on its own.
        _, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for pump in pending:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
    finally:
        await upstream.close()
        try:
            await _close_websocket(send, 1000)
        except RuntimeError:
            # Already closed from the other end, which is the common case.
            pass


async def handle(scope: Scope, receive: Receive, send: Send) -> None:
    """Serve one request under `/vscode/<project>/<workspace>/` from that workspace's editor."""
    is_websocket = scope["type"] == ScopeType.WEBSOCKET
    target = target_of(scope)

    if target is None:
        if is_websocket:
            await receive()
            await _close_websocket(send, 1008)
            return
        await _send_json(send, 404, {"error": "not_found", "message": "not an editor path"})
        return

    instance = instances.running(target.workspace.id)
    if instance is None:
        # A cold start takes seconds, so it belongs behind an explicit POST /api/editor where the
        # client can show progress, rather than inside a page load that would just look hung.
        if is_websocket:
            await receive()
            await _close_websocket(send, 1011)
            return
        message = f"no editor running for {target.workspace.id}"
        await _send_json(send, 503, {"error": "not_running", "message": message})
        return

    instances.touch(target.workspace.id)
    if is_websocket:
        await _forward_ws(instance, target, cast("WebSocketScope", scope), receive, send)
    else:
        await _forward_http(instance, target, cast("HTTPScope", scope), receive, send)
