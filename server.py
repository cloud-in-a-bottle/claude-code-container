"""claude-workbench: tabbed web terminals + Claude Code, for building/debugging openhost apps.

Routes:
    GET  /                         -> tabbed terminal UI
    GET  /health                   -> health check
    GET  /terminal/ws              -> WebSocket PTY (one session per connection)
    POST /api/sessions             -> queue a prefilled Claude session; returns {id, url}
                                      body: {"prompt": "...", "context": "..."}
                                      the next WS that connects with ?session=<id> picks it up
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import secrets
import signal
import struct
import subprocess
import termios
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import tomli_w
from quart import Quart, jsonify, request, send_from_directory, websocket

APP_DIR = Path(__file__).parent
HOME = Path(os.environ.get("HOME", "/root"))
OPENHOST_DIR = Path(os.environ.get("OPENHOST_DIR", str(HOME / "openhost")))

ROUTER_URL = os.environ.get("OPENHOST_ROUTER_URL", "")
APP_TOKEN = os.environ.get("OPENHOST_APP_TOKEN", "")
SECRETS_SHORTNAME = "secrets"

app = Quart(__name__, template_folder=str(APP_DIR / "templates"), static_folder=str(APP_DIR / "static"))

# Cached ANTHROPIC_API_KEY value, fetched lazily from the secrets app on first
# PTY launch. `None` means "not yet fetched"; "" means "tried, not available".
_anthropic_key: str | None = None
_anthropic_lock = asyncio.Lock()


async def _fetch_secrets(keys: list[str]) -> dict[str, str]:
    """Ask the secrets-v2 app for the given keys. Returns {} if unavailable."""
    if not ROUTER_URL or not APP_TOKEN:
        return {}
    url = f"{ROUTER_URL}/api/services/v2/call/{SECRETS_SHORTNAME}/get"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                url,
                json={"keys": keys},
                headers={"Authorization": f"Bearer {APP_TOKEN}"},
            )
        if resp.status_code != 200:
            return {}
        return {k: v for k, v in (resp.json().get("secrets") or {}).items() if v}
    except Exception:
        return {}


async def _fetch_anthropic_key() -> str:
    """Ask the secrets-v2 app for ANTHROPIC_API_KEY. Returns "" if unavailable."""
    return (await _fetch_secrets(["ANTHROPIC_API_KEY"])).get("ANTHROPIC_API_KEY", "")


async def _seed_oh_config() -> None:
    """Best-effort: write ~/.openhost/compute_space_cli.toml from env + secrets.

    Hostname comes from OPENHOST_ZONE_DOMAIN, which openhost injects into every
    app's env. The user-bound API token isn't auto-provided, so we fetch
    OH_TOKEN from secrets-v2. If the config file already exists (user
    configured manually), don't overwrite it.
    """
    cfg_path = HOME / ".openhost" / "compute_space_cli.toml"
    if cfg_path.exists():
        return
    hostname = os.environ.get("OPENHOST_ZONE_DOMAIN", "").strip()
    if not hostname:
        return
    token = (await _fetch_secrets(["OH_TOKEN"])).get("OH_TOKEN", "").strip()
    if not token:
        return
    # Strip any protocol/path in case the zone domain is ever stored with one.
    hostname = hostname.replace("https://", "").replace("http://", "").rstrip("/")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        tomli_w.dumps(
            {
                "default_instance": hostname,
                "instances": {hostname: {"token": token}},
            }
        )
    )


async def _get_anthropic_key() -> str:
    """Return the cached key, fetching once if we haven't yet (or last attempt failed)."""
    global _anthropic_key
    if _anthropic_key:
        return _anthropic_key
    async with _anthropic_lock:
        if _anthropic_key:
            return _anthropic_key
        key = await _fetch_anthropic_key()
        if key:
            _anthropic_key = key
        return key


@dataclass
class PendingSession:
    """A prefilled session waiting for a websocket to attach.

    `command` runs in the PTY at startup. `stdin_seed` is fed to its stdin
    once after launch (used to send a multi-line prompt to `claude`).
    """

    command: list[str]
    stdin_seed: str = ""
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)


_pending: dict[str, PendingSession] = {}


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


@app.get("/health")
async def health() -> tuple[dict, int]:
    return {"status": "ok"}, 200


@app.get("/")
async def index() -> object:
    return await send_from_directory(str(APP_DIR / "templates"), "index.html")


@app.post("/api/sessions")
async def create_session() -> object:
    """Reserve a prefilled Claude session.

    Returns an id; the frontend opens a new tab whose websocket includes
    ?session=<id>, and we launch `claude` there with the prompt piped in.
    """
    data = await request.get_json(silent=True) or {}
    prompt: str = (data.get("prompt") or "").strip()
    context: str = (data.get("context") or "").strip()
    if not prompt and not context:
        return jsonify({"error": "prompt or context required"}), 400

    seed_parts: list[str] = []
    if context:
        seed_parts.append(f"# Context\n\n{context}\n")
    if prompt:
        seed_parts.append(prompt)
    seed = "\n\n".join(seed_parts) + "\n"

    sid = secrets.token_urlsafe(8)
    _pending[sid] = PendingSession(
        command=["claude", "--dangerously-skip-permissions"],
        stdin_seed=seed,
        cwd=str(OPENHOST_DIR if OPENHOST_DIR.exists() else HOME),
    )
    return jsonify({"id": sid, "url": f"/?session={sid}"})


@app.websocket("/terminal/ws")
async def terminal_ws() -> None:
    session_id = websocket.args.get("session")
    pending = _pending.pop(session_id, None) if session_id else None

    if pending is not None:
        command = pending.command
        cwd = pending.cwd
        extra_env = dict(pending.env)
        stdin_seed = pending.stdin_seed
    else:
        command = ["bash", "-l"]
        cwd = str(OPENHOST_DIR) if OPENHOST_DIR.exists() else str(HOME)
        extra_env = {}
        stdin_seed = ""

    # Pre-populate ANTHROPIC_API_KEY from the secrets app if available and the
    # caller hasn't already set one.
    if "ANTHROPIC_API_KEY" not in extra_env:
        key = await _get_anthropic_key()
        if key:
            extra_env["ANTHROPIC_API_KEY"] = key

    await _bridge_pty(command=command, cwd=cwd, extra_env=extra_env, stdin_seed=stdin_seed)


async def _bridge_pty(
    *, command: list[str], cwd: str | None, extra_env: dict[str, str], stdin_seed: str
) -> None:
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, 24, 80)

    env = {**os.environ, "TERM": "xterm-256color", **extra_env}
    proc = subprocess.Popen(  # noqa: S603
        command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=cwd,
        env=env,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)

    if stdin_seed:
        # Give the child a moment to set up its TTY before we feed input.
        async def _seed() -> None:
            await asyncio.sleep(0.5)
            try:
                os.write(master_fd, stdin_seed.encode())
            except OSError:
                pass

        asyncio.create_task(_seed())

    loop = asyncio.get_event_loop()

    async def pty_to_ws() -> None:
        try:
            while True:
                data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                if not data:
                    break
                await websocket.send(data)
        except Exception:
            pass

    async def ws_to_pty() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if isinstance(msg, (bytes, bytearray)) and len(msg) > 0:
                    kind = msg[0]
                    payload = bytes(msg[1:])
                    if kind == 0x00:
                        os.write(master_fd, payload)
                    elif kind == 0x01:
                        ctrl = json.loads(payload)
                        if ctrl.get("type") == "resize":
                            _set_winsize(master_fd, int(ctrl["rows"]), int(ctrl["cols"]))
                elif isinstance(msg, str):
                    os.write(master_fd, msg.encode())
        except Exception:
            pass

    def cleanup() -> None:
        try:
            os.kill(proc.pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except OSError:
                pass

    tasks = [asyncio.create_task(pty_to_ws()), asyncio.create_task(ws_to_pty())]
    try:
        _, pending_tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending_tasks:
            t.cancel()
    finally:
        cleanup()


async def _serve() -> None:
    import hypercorn.asyncio
    import hypercorn.config

    await _seed_oh_config()

    cfg = hypercorn.config.Config()
    cfg.bind = ["0.0.0.0:5000"]
    cfg.accesslog = "-"
    await hypercorn.asyncio.serve(app, cfg)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
