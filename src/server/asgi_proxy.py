import asyncio
import json
from collections.abc import Callable
from typing import cast

import httpx
from litestar.types import HTTPRequestEvent
from litestar.types import HTTPResponseBodyEvent
from litestar.types import HTTPResponseStartEvent
from litestar.types import HTTPScope
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send
from litestar.types import WebSocketCloseEvent
from litestar.types import WebSocketSendEvent
from websockets.asyncio.client import ClientConnection

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

type Headers = list[tuple[str, str]]


def raw_path_of(scope: Scope) -> str:
    """The path exactly as the browser sent it.

    Not the ASGI `path`: Litestar's mount rewrites that one and appends a trailing slash to it, which
    would turn `/static/main.js` into `/static/main.js/`, and it has also been percent-decoded. Some
    servers leave the query string on the raw path; it is dropped, since it travels separately.
    """
    raw_path = scope.get("raw_path") or scope["path"].encode()
    return bytes(raw_path).decode("utf-8", "replace").partition("?")[0]


def request_headers(scope: Scope) -> Headers:
    return [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in scope["headers"]
        if key.decode("latin-1").lower() not in HOP_BY_HOP
    ]


async def send_json(send: Send, status: int, body: dict[str, str]) -> None:
    await send_body(send, status, "application/json", json.dumps(body).encode())


async def send_body(send: Send, status: int, content_type: str, body: bytes) -> None:
    start: HTTPResponseStartEvent = {
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", content_type.encode()), (b"cache-control", b"no-store")],
    }
    await send(start)
    payload: HTTPResponseBodyEvent = {"type": "http.response.body", "body": body, "more_body": False}
    await send(payload)


async def close_websocket(send: Send, code: int) -> None:
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


async def forward_http(
    client: httpx.AsyncClient,
    url: str,
    headers: Headers,
    scope: HTTPScope,
    receive: Receive,
    send: Send,
    rewrite_header: Callable[[str, str], str] | None = None,
) -> None:
    """Send one request upstream and stream the response back to the browser as it arrives.

    `rewrite_header(name, value)` gets a chance to change each response header on its way out, eg to
    point a redirect back through the proxy. Raises httpx's errors if upstream can't be reached, so
    the caller can say what was down.
    """
    request = client.build_request(scope["method"], url, headers=headers, content=await _request_body(receive))
    response = await client.send(request, stream=True)
    try:
        start: HTTPResponseStartEvent = {
            "type": "http.response.start",
            "status": response.status_code,
            "headers": [
                (key.encode("latin-1"), (rewrite_header(key, value) if rewrite_header else value).encode("latin-1"))
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


async def pump_websocket(upstream: ClientConnection, receive: Receive, send: Send) -> None:
    """Shuttle frames both ways between an accepted browser socket and `upstream` until either closes."""

    async def browser_to_upstream() -> None:
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            frame = cast("dict[str, bytes | str | None]", message)
            data = frame.get("bytes")
            await upstream.send(data if data is not None else str(frame.get("text") or ""))

    async def upstream_to_browser() -> None:
        async for message in upstream:
            frame: WebSocketSendEvent = (
                {"type": "websocket.send", "bytes": message, "text": None}
                if isinstance(message, bytes)
                else {"type": "websocket.send", "bytes": None, "text": message}
            )
            await send(frame)

    pumps = [asyncio.create_task(browser_to_upstream()), asyncio.create_task(upstream_to_browser())]
    try:
        # Either direction closing ends the connection; both ends are expected to reconnect on their own.
        _, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for pump in pending:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
    finally:
        await upstream.close()
        try:
            await close_websocket(send, 1000)
        except RuntimeError:
            # Already closed from the other end, which is the common case.
            pass
