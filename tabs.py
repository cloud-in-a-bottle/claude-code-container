import asyncio
import fcntl
import json
import os
import pty
import secrets
import shlex
import shutil
import signal
import struct
import subprocess
import termios
from pathlib import Path

import attr
from quart import websocket

from config import HOME, MY_PROJECT_DIR
from remote_services import get_anthropic_key
from tab_store import CLAUDE, SHELL, PersistedTab, load_tabs, save_tabs

_BUF_MAX = 100 * 1024  # 100 KB ring buffer per tab
_KICKED_MSG = b"\x01" + json.dumps({"type": "kicked"}).encode()

_tabs: dict[str, "ServerTab"] = {}
_tab_counter: int = 0
_claude_tab_created: bool = False
_active_cwd: str | None = None  # set via set_active_cwd(); used as cwd for all new tabs
_last_persisted: list["PersistedTab"] = []


def set_active_cwd(path: str) -> None:
    """Point all future tabs at a new working directory and mark the Claude tab as claimed."""
    global _active_cwd, _claude_tab_created
    _active_cwd = path
    _claude_tab_created = True


def _proc_name(pgid: int) -> str:
    """Return a human-readable process name for the PGID, or '' for shells/unknowns."""
    try:
        with open(f"/proc/{pgid}/comm") as f:
            comm = f.read().strip()
    except OSError:
        return ""
    if comm in ("bash", "sh", "dash"):
        return ""
    if comm == "node":
        try:
            with open(f"/proc/{pgid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
            if "claude" in cmdline.lower():
                return "claude"
        except OSError:
            pass
        return ""
    return comm


def tab_proc_info(tab: "ServerTab") -> tuple[str, str]:
    """Return (program, cwd) for the foreground process of a tab's PTY.

    program is '' when the foreground is bash/unknown.
    cwd uses '~' for the home directory prefix.
    """
    if not tab.alive:
        return "", ""
    try:
        pgid = os.tcgetpgrp(tab.master_fd)
    except OSError:
        return "", ""
    if pgid <= 0:
        return "", ""

    program = _proc_name(pgid)

    cwd = ""
    try:
        raw = os.readlink(f"/proc/{pgid}/cwd")
        home_s = str(HOME)
        if raw == home_s:
            cwd = "~"
        elif raw.startswith(home_s + "/"):
            cwd = "~" + raw[len(home_s) :]
        else:
            cwd = raw
    except OSError:
        pass

    return program, cwd


@attr.s(auto_attribs=True)
class ServerTab:
    id: str
    label: str
    master_fd: int
    proc: subprocess.Popen[bytes]
    # What to bring back if this tab is restored: a Claude session or a plain shell, and where.
    # start_cwd is the fallback for when the live cwd can't be read (the process has exited).
    kind: str = SHELL
    start_cwd: str = str(HOME)
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
            persist_tabs()
            if tab.client_queue is not None:
                try:
                    tab.client_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            return
        if not data:
            tab.alive = False
            persist_tabs()
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
    kind: str = SHELL,
    tab_id: str | None = None,
) -> ServerTab:
    """Start a tab. `kind` is what a restore should recreate, not necessarily what `command` runs.

    A bootstrap script that clones a repo and then launches Claude is kind=CLAUDE: restoring it
    re-enters the conversation rather than cloning again. `tab_id` is only passed when restoring,
    so a client holding a ?tab= link still resolves after a restart.
    """
    global _tab_counter
    _tab_counter += 1
    tab_id = tab_id or secrets.token_urlsafe(8)
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

    tab = ServerTab(
        id=tab_id,
        label=tab_label,
        master_fd=master_fd,
        proc=proc,
        kind=kind,
        start_cwd=cwd or str(HOME),
    )
    _tabs[tab_id] = tab

    asyncio.create_task(tab_reader(tab))
    persist_tabs()

    if stdin_seed:

        async def _seed() -> None:
            await asyncio.sleep(0.5)
            try:
                os.write(master_fd, stdin_seed.encode())
            except OSError:
                pass

        asyncio.create_task(_seed())

    return tab


def _tab_cwd(tab: ServerTab) -> str:
    """Where this tab currently is, falling back to where it started once the process is gone."""
    _, cwd = tab_proc_info(tab)
    if not cwd:
        return tab.start_cwd
    if cwd == "~":
        return str(HOME)
    if cwd.startswith("~/"):
        return str(HOME / cwd[2:])
    return cwd


def tab_snapshot() -> list[PersistedTab]:
    """The live tabs, in the form a restore needs. Dead tabs are dropped rather than resurrected."""
    return [
        PersistedTab(id=t.id, label=t.label, kind=t.kind, cwd=_tab_cwd(t)) for t in _tabs.values() if t.alive
    ]


def persist_tabs() -> None:
    """Write the tab list, skipping the write when nothing has changed since last time."""
    global _last_persisted
    snapshot = tab_snapshot()
    if snapshot == _last_persisted:
        return
    try:
        save_tabs(snapshot)
    except OSError as e:
        # Losing the tab list is a lot better than taking the workbench down over it.
        print(f"[tabs] could not save the tab list: {e}", flush=True)
        return
    _last_persisted = snapshot


async def persist_tabs_periodically(interval_seconds: float = 30.0) -> None:
    """Keep the saved cwds current — they drift as the user moves around, with no event to hook."""
    while True:
        await asyncio.sleep(interval_seconds)
        persist_tabs()


def restore_command(kind: str, claude_bin: str) -> list[str]:
    """The command that brings a tab of this kind back.

    Claude tabs re-enter the last conversation for that directory; `--continue` exits non-zero when
    there isn't one (a brand new checkout, or a cleared history), so fall through to a fresh
    session rather than dumping the user at a bare shell.
    """
    if kind != CLAUDE:
        return ["bash", "-l"]
    claude = shlex.quote(claude_bin)
    return [
        "bash",
        "-l",
        "-c",
        f"{claude} --continue --dangerously-skip-permissions "
        f"|| {claude} --dangerously-skip-permissions; exec bash",
    ]


async def restore_tabs() -> list[ServerTab]:
    """Recreate the tabs from the last run. Processes don't survive a restart; their tabs do.

    Called once at startup, before any client connects.
    """
    global _claude_tab_created
    persisted = load_tabs()
    if not persisted:
        return []

    key = await get_anthropic_key()
    env: dict[str, str] = {"ANTHROPIC_API_KEY": key} if key else {}
    claude_bin = shutil.which("claude") or "claude"

    restored: list[ServerTab] = []
    for entry in persisted:
        # The directory can be gone — a repo removed, or a temp dir cleaned up between runs.
        cwd = entry.cwd if Path(entry.cwd).is_dir() else str(HOME)
        restored.append(
            await create_server_tab(
                command=restore_command(entry.kind, claude_bin),
                cwd=cwd,
                env=env,
                label=entry.label,
                kind=entry.kind,
                tab_id=entry.id,
            )
        )
    # Whatever the restored mix is, the "first tab runs Claude" rule has already had its say.
    _claude_tab_created = True
    print(f"[tabs] restored {len(restored)} tab(s) from the previous run", flush=True)
    return restored


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
    persist_tabs()


async def new_bash_tab(label: str | None = None) -> ServerTab:
    """Create a new tab. The first tab runs Claude Code; all others open plain bash in HOME."""
    global _claude_tab_created
    if not _claude_tab_created:
        _claude_tab_created = True
        key = await get_anthropic_key()
        env: dict[str, str] = {}
        if key:
            env["ANTHROPIC_API_KEY"] = key
        claude_bin = shutil.which("claude") or "claude"
        cwd = _active_cwd or (str(MY_PROJECT_DIR) if MY_PROJECT_DIR.exists() else str(HOME))
        return await create_server_tab(
            command=[
                "bash",
                "-l",
                "-c",
                f"for _i in 1 2 3; do {claude_bin} --dangerously-skip-permissions && break; sleep 1; done; exec bash",
            ],
            cwd=cwd,
            env=env,
            label=label,
            kind=CLAUDE,
        )
    else:
        return await create_server_tab(
            command=["bash", "-l"],
            cwd=_active_cwd or str(HOME),
            label=label,
            kind=SHELL,
        )


async def handle_terminal_ws() -> None:
    await websocket.accept()
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

    # Wait for the client's initial resize before replaying the output buffer.
    # The client always sends resize in ws.onopen (active tabs use measured size,
    # background tabs use last known size). Without this, output_buf would be
    # written into a terminal that hasn't been sized yet, causing garbled display.
    try:
        first_msg = await asyncio.wait_for(websocket.receive(), timeout=1.0)
        if isinstance(first_msg, bytes | bytearray) and len(first_msg) > 1:
            if first_msg[0] == 0x01:
                try:
                    ctrl = json.loads(bytes(first_msg[1:]))
                    if ctrl.get("type") == "resize":
                        set_winsize(tab.master_fd, int(ctrl["rows"]), int(ctrl["cols"]))
                except Exception:
                    pass
            elif first_msg[0] == 0x00:
                os.write(tab.master_fd, bytes(first_msg[1:]))
    except (TimeoutError, Exception):
        pass

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
