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
import uuid
from pathlib import Path
from typing import Any

import attr
from litestar import WebSocket

from server.claude_sessions import latest_session_id
from server.config import HOME
from server.projects.workspaces import Workspace
from server.projects.workspaces import parse_workspace_id
from server.remote_services import get_anthropic_key
from server.signals import reset_child_signals
from server.tab_store import CLAUDE
from server.tab_store import SHELL
from server.tab_store import PersistedTab
from server.tab_store import load_tabs
from server.tab_store import save_tabs

_BUF_MAX = 100 * 1024  # 100 KB ring buffer per tab
_KICKED_MSG = b"\x01" + json.dumps({"type": "kicked"}).encode()

_tabs: dict[str, ServerTab] = {}
_tab_counter: int = 0
_last_persisted: list[PersistedTab] = []
_restoring: bool = False  # see restore_tabs(): suppresses the partial writes a restore would make


def new_session_id() -> str:
    """A stable id for one tab's Claude conversation. Must be a UUID; `claude --session-id` demands it."""
    return str(uuid.uuid4())


def claude_session_command(claude_bin: str, session_id: str, *, resume_first: bool) -> str:
    """Shell snippet that lands the user in `session_id`, whether or not it exists yet.

    Both orderings work — the loser of the pair just exits non-zero — so the order only decides
    whether the user sees a spurious error first. Lead with resume when the session is expected to
    exist (a restore) and with create when it isn't (a brand new tab).
    """
    claude = shlex.quote(claude_bin)
    sid = shlex.quote(session_id)
    create = f"{claude} --session-id {sid} --dangerously-skip-permissions"
    resume = f"{claude} --resume {sid} --dangerously-skip-permissions"
    return f"{resume} || {create}" if resume_first else f"{create} || {resume}"


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


def tab_proc_info(tab: ServerTab) -> tuple[str, str]:
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
    session_id: str = ""
    # The workspace this tab belongs to, as `<project>/<workspace>`. Tabs are only ever shown in
    # their own workspace, and a tab whose workspace has been deleted is not restored.
    workspace_id: str = ""
    lock: asyncio.Lock = attr.Factory(asyncio.Lock)
    connected: bool = False
    alive: bool = True
    output_buf: bytearray = attr.Factory(bytearray)
    client_queue: asyncio.Queue[bytes | None] | None = None
    kick_event: asyncio.Event | None = None


def _prepare_tab_process() -> None:
    """Runs in the forked child before exec: its own session, and the signal defaults back."""
    os.setsid()
    reset_child_signals()


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
    session_id: str = "",
    workspace_id: str = "",
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

    # xterm.js renders 24-bit SGR, but nothing in the pty advertises that, so TERM alone leaves
    # apps at the 256-colour tier: Claude Code quantises its theme onto the xterm cube and its
    # own /doctor asks for COLORTERM. Terminals we mirror (iTerm2, kitty, VS Code) all set it.
    merged_env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor", **(env or {})}
    proc = subprocess.Popen(  # noqa: S603
        command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=cwd,
        env=merged_env,
        preexec_fn=_prepare_tab_process,
    )
    os.close(slave_fd)

    tab = ServerTab(
        id=tab_id,
        label=tab_label,
        master_fd=master_fd,
        proc=proc,
        kind=kind,
        start_cwd=cwd or str(HOME),
        session_id=session_id,
        workspace_id=workspace_id,
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
        PersistedTab(
            id=t.id,
            label=t.label,
            kind=t.kind,
            cwd=_tab_cwd(t),
            session_id=t.session_id,
            workspace_id=t.workspace_id,
        )
        for t in _tabs.values()
        if t.alive
    ]


def persist_tabs() -> None:
    """Write the tab list, skipping the write when nothing has changed since last time."""
    global _last_persisted
    # Mid-restore the live set is only the tabs rebuilt so far, and create_server_tab() would
    # persist that partial list over the full one. Dying in that window would lose the rest.
    if _restoring:
        return
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


def restore_command(kind: str, claude_bin: str, session_id: str = "", *, continue_ok: bool = True) -> list[str]:
    """The command that brings a tab of this kind back.

    A tab with a session id reattaches to that exact conversation, so two Claude tabs in one
    directory come back as two distinct conversations. Without one, `--continue` is the best
    available: it resolves per-directory, so it can only be trusted when the tab is landing in the
    directory it left; `continue_ok=False` says it isn't. Every branch falls through to a fresh
    session rather than dumping the user at a bare shell.
    """
    if kind != CLAUDE:
        return ["bash", "-l"]
    claude = shlex.quote(claude_bin)
    if session_id:
        attempt = claude_session_command(claude_bin, session_id, resume_first=True)
    elif continue_ok:
        attempt = f"{claude} --continue --dangerously-skip-permissions || {claude} --dangerously-skip-permissions"
    else:
        attempt = f"{claude} --dangerously-skip-permissions"
    return ["bash", "-l", "-c", f"{attempt}; exec bash"]


async def restore_tabs() -> list[ServerTab]:
    """Recreate the tabs from the last run. Processes don't survive a restart; their tabs do.

    Called once at startup, before any client connects.
    """
    global _restoring
    persisted = load_tabs()
    if not persisted:
        return []

    key = await get_anthropic_key()
    env: dict[str, str] = {"ANTHROPIC_API_KEY": key} if key else {}
    claude_bin = shutil.which("claude") or "claude"

    restored: list[ServerTab] = []
    _restoring = True
    try:
        for entry in persisted:
            workspace = parse_workspace_id(entry.workspace_id)
            # A tab is only meaningful inside its workspace, so one whose workspace has been
            # deleted (or which predates workspaces entirely) is dropped rather than resurrected
            # somewhere arbitrary.
            if workspace is None or not workspace.path.is_dir():
                print(f"[tabs] dropping tab {entry.label!r}: workspace {entry.workspace_id!r} is gone", flush=True)
                continue
            # The tab's own cwd can still be gone even when the workspace isn't — a subdirectory
            # it was sitting in got deleted, say.
            cwd_ok = Path(entry.cwd).is_dir()
            restored.append(
                await create_server_tab(
                    command=restore_command(entry.kind, claude_bin, entry.session_id, continue_ok=cwd_ok),
                    cwd=entry.cwd if cwd_ok else str(workspace.path),
                    env=env,
                    label=entry.label,
                    kind=entry.kind,
                    tab_id=entry.id,
                    session_id=entry.session_id,
                    workspace_id=entry.workspace_id,
                )
            )
    finally:
        _restoring = False
    persist_tabs()
    print(f"[tabs] restored {len(restored)} tab(s) from the previous run", flush=True)
    return restored


def tab_pgid(tab: ServerTab) -> int:
    """The process group to signal to end this tab, checked before anything is sent to it.

    create_server_tab() runs the child under os.setsid(), so it leads its own group and its pid is
    also its pgid. The validation is not paranoia: a signal aimed at a pid of 0 or 1 hits the whole
    server (pid 1 in the container is tini, which forwards it), so a bad pid here takes the
    workbench down instead of one terminal. Better to raise and lose one tab.
    """
    pid = tab.proc.pid
    if not isinstance(pid, int) or pid <= 1:
        raise ValueError(f"refusing to signal pid {pid!r} for tab {tab.id}: not a tab process")
    return pid


def kill_tab(tab: ServerTab) -> None:
    """Kill the PTY's process group and close the master fd.

    The group, not just the child: the shell's children (Claude, a dev server) would otherwise
    outlive the tab that owned them.
    """
    pgid = tab_pgid(tab)
    try:
        os.killpg(pgid, signal.SIGHUP)
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
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    if tab.client_queue is not None:
        try:
            tab.client_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
    persist_tabs()


def tabs_for_workspace(workspace_id: str) -> list[ServerTab]:
    return [t for t in _tabs.values() if t.workspace_id == workspace_id]


async def new_tab_in_workspace(workspace: Workspace, label: str | None = None) -> ServerTab:
    """Open a tab in a workspace. The workspace's first tab runs Claude; the rest are plain bash."""
    cwd = str(workspace.path)
    live = [t for t in tabs_for_workspace(workspace.id) if t.alive]
    if live:
        return await create_server_tab(
            command=["bash", "-l"],
            cwd=cwd,
            label=label,
            kind=SHELL,
            workspace_id=workspace.id,
        )

    key = await get_anthropic_key()
    env: dict[str, str] = {"ANTHROPIC_API_KEY": key} if key else {}
    claude_bin = shutil.which("claude") or "claude"
    # Prefer the conversation this workspace already has. Reaching here with one on disk means the
    # tab list was lost while the transcript survived -- the tab list is the fragile half, since it
    # only records tabs that were live at the last write. Minting a fresh id in that situation
    # orphans real work: the conversation stays on disk with nothing pointing at it, and the user
    # gets an empty Claude in a workspace they had been talking to.
    adopted = latest_session_id(cwd)
    session_id = adopted or new_session_id()
    # A retry that follows a crash must rejoin the session the crashed attempt created, not
    # collide with it — claude rejects --session-id for an id that already exists. Same pair of
    # commands either way; only the order changes, so the user doesn't see a spurious error first.
    attempt = claude_session_command(claude_bin, session_id, resume_first=bool(adopted))
    return await create_server_tab(
        command=["bash", "-l", "-c", f"for _i in 1 2 3; do {{ {attempt}; }} && break; sleep 1; done; exec bash"],
        cwd=cwd,
        env=env,
        label=label or "claude",
        kind=CLAUDE,
        session_id=session_id,
        workspace_id=workspace.id,
    )


async def handle_terminal_ws(socket: WebSocket[Any, Any, Any]) -> None:
    await socket.accept()
    tab_id = socket.query_params.get("tab")
    tab = _tabs.get(tab_id) if tab_id else None
    if tab is None:
        return
    if tab.lock.locked():
        await socket.send_data(b"\x01" + json.dumps({"type": "busy"}).encode(), mode="binary")
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
        first_msg = await asyncio.wait_for(socket.receive_data(mode="binary"), timeout=1.0)
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
    except Exception:
        pass

    if tab.output_buf:
        await socket.send_data(b"\x00" + bytes(tab.output_buf), mode="binary")

    async def pty_to_ws() -> None:
        try:
            while True:
                chunk = await q.get()
                if chunk is None:
                    await socket.send_data(b"\x01" + json.dumps({"type": "exit"}).encode(), mode="binary")
                    break
                if chunk is _KICKED_MSG:
                    await socket.send_data(_KICKED_MSG, mode="binary")
                    break
                await socket.send_data(b"\x00" + chunk, mode="binary")
        except Exception:
            pass

    async def ws_to_pty() -> None:
        try:
            while True:
                msg = await socket.receive_data(mode="binary")
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
