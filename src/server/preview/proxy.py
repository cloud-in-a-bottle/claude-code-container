from pathlib import Path
from string import Template
from urllib.parse import urlsplit

import httpx
import websockets
from litestar.enums import ScopeType
from litestar.types import HTTPScope
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from litestar.types import WebSocketAcceptEvent
from litestar.types import WebSocketScope
from websockets.asyncio.client import connect
from websockets.typing import Subprotocol

from server import asgi_proxy
from server.preview.target import UPSTREAM_HOST
from server.preview.target import Mode
from server.preview.target import Target
from server.preview.target import parse_target
from server.preview.target import rewrite_location

_WAITING_PAGE = Template((Path(__file__).parent / "waiting.html").read_text())

# Part of the websocket handshake itself, which the upstream connection does over again.
_HANDSHAKE_HEADERS = frozenset(
    {"host", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"}
)

_client: httpx.AsyncClient | None = None


def _http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # trust_env off: an HTTP(S)_PROXY in the environment is for reaching the internet, and would
        # otherwise be handed requests for localhost. Reads never time out, so a long poll or an
        # event stream from the dev server stays open; connecting does, so a dead port fails fast.
        _client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5), follow_redirects=False, trust_env=False)
    return _client


def _upstream_headers(target: Target, scope: Scope) -> asgi_proxy.Headers:
    """The browser's headers, readdressed so the dev server believes it is being visited locally.

    Host and a same-origin Origin are swapped for the server's own address, since dev servers refuse
    unfamiliar ones (Vite's host check, Next's dev-origin check) and these are the owner's requests
    to their own server. The real address goes along as X-Forwarded-*, for servers that use it to
    build absolute URLs.
    """
    headers = asgi_proxy.request_headers(scope)
    own_host = next((value for key, value in headers if key.lower() == "host"), "")
    local = f"{UPSTREAM_HOST}:{target.port}"
    out: asgi_proxy.Headers = []
    for key, value in headers:
        name = key.lower()
        if name == "host":
            continue
        if name == "origin" and urlsplit(value).netloc == own_host:
            value = f"http://{local}"
        out.append((key, value))
    names = {key.lower() for key, _ in out}
    out.append(("host", local))
    if own_host and "x-forwarded-host" not in names:
        out.append(("x-forwarded-host", own_host))
    if "x-forwarded-proto" not in names:
        out.append(("x-forwarded-proto", "https" if scope.get("scheme") in ("https", "wss") else "http"))
    if target.mode is Mode.STRIP:
        out.append(("x-forwarded-prefix", target.prefix))
    return out


def _wants_html(scope: Scope) -> bool:
    accept = next((v for k, v in scope["headers"] if k.lower() == b"accept"), b"")
    return b"text/html" in accept


async def _forward_http(target: Target, scope: HTTPScope, receive: Receive, send: Send) -> None:
    query = scope["query_string"].decode()
    url = f"http://{UPSTREAM_HOST}:{target.port}{target.upstream_path}" + (f"?{query}" if query else "")

    def rewrite(name: str, value: str) -> str:
        return rewrite_location(target, value) if name.lower() == "location" else value

    try:
        await asgi_proxy.forward_http(
            _http_client(), url, _upstream_headers(target, scope), scope, receive, send, rewrite
        )
    except httpx.ConnectError:
        # Usually the server just hasn't finished starting, so a page load gets a page that keeps
        # trying rather than a dead end -- the panel can be pointed here before the server is up.
        if _wants_html(scope):
            page = _WAITING_PAGE.substitute(port=target.port)
            await asgi_proxy.send_body(send, 502, "text/html; charset=utf-8", page.encode())
        else:
            message = f"nothing is listening on port {target.port}"
            await asgi_proxy.send_json(send, 502, {"error": "upstream_down", "message": message})


async def _forward_ws(target: Target, scope: WebSocketScope, receive: Receive, send: Send) -> None:
    query = scope["query_string"].decode()
    url = f"ws://{UPSTREAM_HOST}:{target.port}{target.upstream_path}" + (f"?{query}" if query else "")
    headers = [(k, v) for k, v in _upstream_headers(target, scope) if k.lower() not in _HANDSHAKE_HEADERS]
    await receive()  # websocket.connect

    try:
        upstream = await connect(
            url,
            additional_headers=headers,
            # Vite's HMR client, for one, only talks to a server that agrees to its subprotocol.
            subprotocols=[Subprotocol(p) for p in scope.get("subprotocols", [])] or None,
            user_agent_header=None,
            proxy=None,
            max_size=None,
            open_timeout=10,
        )
    except (OSError, websockets.InvalidHandshake, TimeoutError) as e:
        print(f"[preview] websocket to port {target.port} failed: {e!r}", flush=True)
        await asgi_proxy.close_websocket(send, 1011)
        return

    accept: WebSocketAcceptEvent = {"type": "websocket.accept", "subprotocol": upstream.subprotocol, "headers": []}
    await send(accept)
    await asgi_proxy.pump_websocket(upstream, receive, send)


async def handle(mode: Mode, scope: Scope, receive: Receive, send: Send) -> None:
    """Serve one request under `/proxy/<port>/` or `/absproxy/<port>/` from that port in the container."""
    target = parse_target(mode, asgi_proxy.raw_path_of(scope))
    if scope["type"] == ScopeType.WEBSOCKET:
        if target is None:
            await receive()
            await asgi_proxy.close_websocket(send, 1008)
            return
        await _forward_ws(target, scope, receive, send)
        return

    if target is None:
        message = f"expected {mode.mount_path}/<port>/..."
        await asgi_proxy.send_json(send, 404, {"error": "not_found", "message": message})
        return
    await _forward_http(target, scope, receive, send)
