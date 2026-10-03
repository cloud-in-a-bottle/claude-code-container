from __future__ import annotations

import json
import socket
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

import pytest
from litestar import Litestar
from litestar.testing import TestClient
from websockets.sync.server import ServerConnection
from websockets.sync.server import serve

from server import app as srv
from server.preview.target import Mode
from server.preview.target import Target
from server.preview.target import parse_target
from server.preview.target import rewrite_location


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


class _Echo(BaseHTTPRequestHandler):
    """Answers every GET with what it was asked, so tests can see what the proxy forwarded."""

    def do_GET(self) -> None:
        if self.path.endswith("/go-home"):
            self.send_response(302)
            self.send_header("Location", f"http://{self.headers['Host']}/home")
            self.end_headers()
            return
        body = json.dumps({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def upstream() -> Generator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---- paths -------------------------------------------------------------------------------------


def test_strip_mode_forwards_only_what_follows_the_port() -> None:
    assert parse_target(Mode.STRIP, "/proxy/3000/a/b.js") == Target(Mode.STRIP, 3000, "/a/b.js")
    assert parse_target(Mode.STRIP, "/proxy/3000/") == Target(Mode.STRIP, 3000, "/")
    assert parse_target(Mode.STRIP, "/proxy/3000") == Target(Mode.STRIP, 3000, "/")


def test_keep_mode_forwards_the_whole_path() -> None:
    target = parse_target(Mode.KEEP, "/absproxy/5173/src/main.ts")
    assert target == Target(Mode.KEEP, 5173, "/absproxy/5173/src/main.ts")


@pytest.mark.parametrize("path", ["/proxy/", "/proxy/abc/", "/proxy/0/", "/proxy/65536/", "/proxy/-1/", "/other/80/"])
def test_paths_that_do_not_name_a_port_are_refused(path: str) -> None:
    assert parse_target(Mode.STRIP, path) is None


def test_a_redirect_to_the_server_itself_comes_back_through_the_proxy() -> None:
    strip = Target(Mode.STRIP, 3000, "/")
    assert rewrite_location(strip, "http://localhost:3000/login?next=/") == "/proxy/3000/login?next=/"
    assert rewrite_location(strip, "http://127.0.0.1:3000") == "/proxy/3000/"
    assert rewrite_location(strip, "/login") == "/proxy/3000/login"
    assert rewrite_location(strip, "login") == "login"

    keep = Target(Mode.KEEP, 5173, "/")
    assert rewrite_location(keep, "http://localhost:5173/absproxy/5173/x") == "/absproxy/5173/x"
    assert rewrite_location(keep, "/absproxy/5173/x") == "/absproxy/5173/x"


def test_a_redirect_somewhere_else_is_left_alone() -> None:
    strip = Target(Mode.STRIP, 3000, "/")
    assert rewrite_location(strip, "https://github.com/login") == "https://github.com/login"
    assert rewrite_location(strip, "http://localhost:4000/") == "http://localhost:4000/"
    assert rewrite_location(strip, "//cdn.example/x") == "//cdn.example/x"


# ---- the proxy ---------------------------------------------------------------------------------


def test_proxy_strips_the_prefix_and_readdresses_the_request(upstream: int) -> None:
    with _client() as client:
        response = client.get(f"/proxy/{upstream}/a/b?q=1", headers={"Host": "workbench.example"})
    assert response.status_code == 200
    seen = response.json()
    assert seen["path"] == "/a/b?q=1"
    assert seen["headers"]["host"] == f"localhost:{upstream}"
    assert seen["headers"]["x-forwarded-host"] == "workbench.example"
    assert seen["headers"]["x-forwarded-prefix"] == f"/proxy/{upstream}"


def test_absproxy_keeps_the_prefix(upstream: int) -> None:
    with _client() as client:
        seen = client.get(f"/absproxy/{upstream}/a/b").json()
    assert seen["path"] == f"/absproxy/{upstream}/a/b"
    assert "x-forwarded-prefix" not in seen["headers"]


def test_a_same_origin_origin_is_readdressed_and_a_foreign_one_is_not(upstream: int) -> None:
    with _client() as client:
        own = client.get(f"/proxy/{upstream}/", headers={"Host": "wb.example", "Origin": "https://wb.example"})
        other = client.get(f"/proxy/{upstream}/", headers={"Host": "wb.example", "Origin": "https://evil.example"})
    assert own.json()["headers"]["origin"] == f"http://localhost:{upstream}"
    assert other.json()["headers"]["origin"] == "https://evil.example"


def test_upstream_redirects_are_rewritten(upstream: int) -> None:
    with _client() as client:
        response = client.get(f"/proxy/{upstream}/go-home", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/proxy/{upstream}/home"


def test_a_page_load_on_a_dead_port_gets_a_page_that_keeps_trying() -> None:
    port = _free_port()
    with _client() as client:
        page = client.get(f"/proxy/{port}/", headers={"Accept": "text/html"})
        api = client.get(f"/proxy/{port}/data.json")
    assert page.status_code == 502
    assert 'http-equiv="refresh"' in page.text
    assert str(port) in page.text
    assert api.status_code == 502
    assert api.json()["error"] == "upstream_down"


def test_a_path_without_a_port_is_a_404() -> None:
    with _client() as client:
        assert client.get("/proxy/nope/").status_code == 404


def test_websockets_go_through_with_their_subprotocol() -> None:
    """Vite's hot reload is a websocket that only talks to a server agreeing to `vite-hmr`."""

    def echo(conn: ServerConnection) -> None:
        for message in conn:
            conn.send(f"{conn.request.path if conn.request else ''} {message!s}")

    with serve(echo, "127.0.0.1", 0, subprotocols=["vite-hmr"]) as server:  # type: ignore[list-item]
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with (
                _client() as client,
                client.websocket_connect(f"/absproxy/{port}/hmr", subprotocols=["vite-hmr"]) as ws,
            ):
                ws.send_text("ping")
                assert ws.receive_text() == f"/absproxy/{port}/hmr ping"
        finally:
            server.shutdown()
