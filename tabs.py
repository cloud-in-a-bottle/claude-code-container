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

import attr
from quart import websocket

from config import HOME, MY_PROJECT_DIR
from remote_services import get_anthropic_key

_BUF_MAX = 100 * 1024  # 100 KB ring buffer per tab
_KICKED_MSG = b"\x01" + json.dumps({"type": "kicked"}).encode()

_tabs: dict[str, "ServerTab"] = {}
_tab_counter: int = 0


@attr.s(auto_attribs=True)
class ServerTab:
    id: str
    label: str
    master_fd: int
    proc: subprocess.Popen[bytes]
    lock: asyncio.Lock = attr.Factory(asyncio.Lock)
    connected: bool = False
    alive: bool = True
    output_buf: bytearray = attr.Factory(bytearray)
    client_queue: asyncio.Queue[bytes | None] | None = None
    kick_event: asyncio.Event | None = None


def set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def tab_reader(tab: ServerTab) -> None:
    """Background task: drain master_fd into the ring buffer and the client queue."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            data = await loop.run_in_executor(None, os.read, tab.master_fd, 4096)
        except OSError:
            tab.alive = False
            if tab.client_queue is not None:
                try:
                    tab.client_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            return
        if not data:
            tab.alive = False
            if tab.client_queue is not None:
                try:
                    tab.client_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            return
        tab.output_buf.extend(data)
        if len(tab.output_buf) > _BUF_MAX:
            del tab.output_buf[: len(tab.output_buf) - _BUF_MAX]
        if tab.client_queue is not None:
            try:
                tab.client_queue.put_nowait(data)
            except asyncio.QueueFull:
                pass


async def create_server_tab(
    *,
    command: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdin_seed: str = "",
    label: str | None = None,
) -> ServerTab:
    global _tab_counter
    _tab_counter += 1
    tab_id = secrets.token_urlsafe(8)
    tab_label = label or f"term {_tab_counter}"

    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd, 24, 80)

    merged_env = {**os.environ, "TERM": "xterm-256color", **(env or {})}
    proc = subprocess.Popen(  # noqa: S603
        command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=cwd,
        env=merged_env,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)

    tab = ServerTab(id=tab_id, label=tab_label, master_fd=master_fd, proc=proc)
    _tabs[tab_id] = tab

    asyncio.create_task(tab_reader(tab))

    if stdin_seed:

        async def _seed() -> None:
            await asyncio.sleep(0.5)
            try:
                os.write(master_fd, stdin_seed.encode())
            except OSError:
                pass

        asyncio.create_task(_seed())

    return tab


def kill_tab(tab: ServerTab) -> None:
    """Kill the PTY process and close the master fd."""
    try:
        os.kill(tab.proc.pid, signal.SIGHUP)
    except OSError:
        pass
    try:
        os.close(tab.master_fd)
    except OSError:
        pass
    try:
        tab.proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.kill(tab.proc.pid, signal.SIGKILL)
        except OSError:
            pass
    if tab.client_queue is not None:
        try:
            tab.client_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


async def new_bash_tab(label: str | None = None) -> ServerTab:
    """Create a new tab running bash -l with claude auto-started."""
    key = await get_anthropic_key()
    env: dict[str, str] = {}
    if key:
        env["ANTHROPIC_API_KEY"] = key
    return await create_server_tab(
        command=["bash", "-l"],
        cwd=str(MY_PROJECT_DIR) if MY_PROJECT_DIR.exists() else str(HOME),
        env=env,
        stdin_seed="claude\n",
        label=label,
    )


async def handle_terminal_ws() -> None:
    tab_id = websocket.args.get("tab")
    tab = _tabs.get(tab_id) if tab_id else None
    if tab is None:
        return
    if tab.lock.locked():
        await websocket.send(b"\x01" + json.dumps({"type": "busy"}).encode())
        return

    await tab.lock.acquire()
    tab.connected = True
    kick = asyncio.Event()
    tab.kick_event = kick
    q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=512)
    tab.client_queue = q

    if tab.output_buf:
        await websocket.send(b"\x00" + bytes(tab.output_buf))

    async def pty_to_ws() -> None:
        try:
            while True:
                chunk = await q.get()
                if chunk is None:
                    await websocket.send(b"\x01" + json.dumps({"type": "exit"}).encode())
                    break
                if chunk is _KICKED_MSG:
                    await websocket.send(_KICKED_MSG)
                    break
                await websocket.send(b"\x00" + chunk)
        except Exception:
            pass

    async def ws_to_pty() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if isinstance(msg, bytes | bytearray) and len(msg) > 0:
                    kind = msg[0]
                    payload = bytes(msg[1:])
                    if kind == 0x00:
                        os.write(tab.master_fd, payload)
                    elif kind == 0x01:
                        ctrl = json.loads(payload)
                        if ctrl.get("type") == "resize":
                            set_winsize(tab.master_fd, int(ctrl["rows"]), int(ctrl["cols"]))
                elif isinstance(msg, str):
                    os.write(tab.master_fd, msg.encode())
        except Exception:
            pass

    async def kick_watcher() -> None:
        await kick.wait()

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(pty_to_ws()),
        asyncio.create_task(ws_to_pty()),
        asyncio.create_task(kick_watcher()),
    ]
    try:
        _, pending_tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending_tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        tab.client_queue = None
        tab.kick_event = None
        tab.connected = False
        tab.lock.release()


async def kick_tab(tab: ServerTab) -> None:
    """Disconnect the current client and wait until the lock is free."""
    if not tab.lock.locked():
        return
    # Notify the connected client before disconnecting it.
    if tab.client_queue is not None:
        try:
            tab.client_queue.put_nowait(_KICKED_MSG)
        except asyncio.QueueFull:
            pass
    # Yield so pty_to_ws can send the notification before we set the kick event.
    await asyncio.sleep(0.05)
    if tab.kick_event is not None:
        tab.kick_event.set()
    await asyncio.wait_for(tab.lock.acquire(), timeout=5.0)
    tab.lock.release()
