import asyncio
import hashlib
import os
import shutil
import signal
import time
from pathlib import Path

import attr
import httpx

from server.editor import paths
from server.editor import settings
from server.editor.install import ensure_installed
from server.projects.workspaces import Workspace
from server.remote_services import get_anthropic_key

# How many code-server instances may run at once. Each one costs 200 MB idle and up to ~1 GB with a
# browser attached, in a container with 4 GB and one core, alongside the Claude sessions that are
# the point of the workbench. Opening a third stops the least recently used one.
MAX_INSTANCES = 2

# code-server shuts itself down after this long with nobody attached, which is what keeps a
# forgotten editor from holding a GB for the rest of the day. The workbench notices it exited and
# starts a fresh one the next time that workspace's editor is opened.
IDLE_TIMEOUT_SECONDS = 1800
# How long a disconnected client has to come back before its extension host is torn down. The
# default is three hours, which in a container this size is indistinguishable from a leak.
RECONNECTION_GRACE_SECONDS = 60

_READY_TIMEOUT_SECONDS = 90.0
_STOP_TIMEOUT_SECONDS = 10.0

_instances: dict[str, EditorInstance] = {}
# One lock per workspace: a double click must not start two instances for the same workspace, and
# starting one for workspace A must not block opening one for workspace B.
_locks: dict[str, asyncio.Lock] = {}


@attr.s(auto_attribs=True)
class EditorInstance:
    workspace_id: str
    socket_path: Path
    user_data_dir: Path
    proc: asyncio.subprocess.Process
    # Refreshed whenever the workspace's editor is used, so eviction picks a genuinely cold one
    # rather than whichever was started first.
    last_used: float
    # The proxy's connection pool to this instance's socket, built on first use. It belongs to the
    # instance so that stopping one closes its connections, rather than leaving a pool pointing at
    # a socket that no longer exists.
    http: httpx.AsyncClient | None = None

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None


def http_client(instance: EditorInstance) -> httpx.AsyncClient:
    if instance.http is None:
        instance.http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(instance.socket_path)),
            timeout=None,
            follow_redirects=False,
        )
    return instance.http


def _digest(workspace_id: str) -> str:
    """A short, filesystem-safe stand-in for a workspace id.

    Not for looks: `<project>/<workspace>` can be 129 characters, and both the socket and the IPC
    socket code-server puts inside the user-data-dir have to fit in the 108-byte AF_UNIX limit.
    """
    return hashlib.sha256(workspace_id.encode()).hexdigest()[:12]


def socket_path(workspace_id: str) -> Path:
    return paths.SOCKETS_DIR / f"{_digest(workspace_id)}.sock"


def user_data_dir(workspace_id: str) -> Path:
    return paths.INSTANCES_DIR / _digest(workspace_id)


def running(workspace_id: str) -> EditorInstance | None:
    """The live instance for a workspace, if there is one. Exited instances are forgotten here."""
    instance = _instances.get(workspace_id)
    if instance is None:
        return None
    if not instance.alive:
        _instances.pop(workspace_id, None)
        return None
    return instance


def running_instances() -> list[EditorInstance]:
    return [i for wid in list(_instances) if (i := running(wid)) is not None]


def touch(workspace_id: str) -> None:
    instance = running(workspace_id)
    if instance is not None:
        instance.last_used = time.monotonic()


async def _evict_until_under_cap() -> None:
    """Make room for one more instance, oldest use first."""
    while len(running_instances()) >= MAX_INSTANCES:
        oldest = min(running_instances(), key=lambda i: i.last_used)
        print(f"[editor] at the {MAX_INSTANCES}-instance cap; stopping {oldest.workspace_id}", flush=True)
        await stop(oldest.workspace_id)


def _command(binary: Path, workspace: Workspace, sock: Path, data_dir: Path) -> list[str]:
    return [
        str(binary),
        # A unix socket, never a port: the editor gets no authentication of its own, and everything
        # that reaches it has already been through the openhost router's auth on the way to us.
        "--socket",
        str(sock),
        "--socket-mode",
        "700",
        "--auth",
        "none",
        "--user-data-dir",
        str(data_dir),
        "--extensions-dir",
        str(paths.EXTENSIONS_DIR),
        # Ours, and empty: without it code-server writes a default config into ~/.config, and a
        # stale one there would quietly outrank nothing but confuse everyone.
        "--config",
        str(paths.CONFIG_PATH),
        "--disable-telemetry",
        "--disable-update-check",
        "--disable-workspace-trust",
        "--disable-getting-started-override",
        # code-server's own /proxy/<port> would otherwise expose every port inside the container,
        # behind the workbench's authentication but well past what an editor panel needs.
        "--disable-proxy",
        "--idle-timeout-seconds",
        str(IDLE_TIMEOUT_SECONDS),
        "--reconnection-grace-time",
        str(RECONNECTION_GRACE_SECONDS),
        "--app-name",
        "workbench",
        # The folder is given explicitly on every start, so a remembered one can't win.
        "--ignore-last-opened",
        str(workspace.path),
    ]


async def _wait_until_ready(sock: Path, proc: asyncio.subprocess.Process) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            raise RuntimeError(f"code-server exited with {proc.returncode} before it was ready")
        try:
            _, writer = await asyncio.open_unix_connection(str(sock))
        except OSError:
            await asyncio.sleep(0.2)
            continue
        writer.close()
        return
    raise TimeoutError(f"code-server did not start listening on {sock} within {_READY_TIMEOUT_SECONDS:.0f}s")


async def _reap(instance: EditorInstance) -> None:
    """Forget an instance once its process is gone — it exits on its own idle timeout, with nothing
    else to tell us."""
    await instance.proc.wait()
    if _instances.get(instance.workspace_id) is instance:
        del _instances[instance.workspace_id]
    instance.socket_path.unlink(missing_ok=True)
    print(f"[editor] {instance.workspace_id} exited ({instance.proc.returncode})", flush=True)


async def start(workspace: Workspace) -> EditorInstance:
    """Start this workspace's editor, or return the one already running for it."""
    lock = _locks.setdefault(workspace.id, asyncio.Lock())
    async with lock:
        existing = running(workspace.id)
        if existing is not None:
            existing.last_used = time.monotonic()
            return existing

        binary = await ensure_installed()
        await _evict_until_under_cap()

        data_dir = user_data_dir(workspace.id)
        data_dir.mkdir(parents=True, exist_ok=True)
        # The directories are named by digest, so leave something behind that says which workspace
        # this one belongs to.
        (data_dir / "workspace").write_text(workspace.id + "\n")
        settings.link_shared_config(data_dir)

        paths.SOCKETS_DIR.mkdir(parents=True, exist_ok=True)
        sock = socket_path(workspace.id)
        # code-server removes its socket on a clean exit; a kill -9 or a container restart leaves
        # one behind, and it would refuse to bind over it.
        sock.unlink(missing_ok=True)

        env = dict(os.environ)
        # code-server's integrated terminals inherit this, and they want the same renderer the
        # workbench's own terminals do -- see the note in tabs.create_server_tab.
        env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] = "1"
        key = await get_anthropic_key()
        if key:
            env["ANTHROPIC_API_KEY"] = key

        proc = await asyncio.create_subprocess_exec(
            *_command(binary, workspace, sock, data_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            # Its own process group, so stopping it takes the extension hosts and language servers
            # with it rather than leaving them parented to the workbench.
            start_new_session=True,
        )
        try:
            await _wait_until_ready(sock, proc)
        except RuntimeError, TimeoutError:
            await _terminate(proc)
            sock.unlink(missing_ok=True)
            raise

        instance = EditorInstance(
            workspace_id=workspace.id,
            socket_path=sock,
            user_data_dir=data_dir,
            proc=proc,
            last_used=time.monotonic(),
        )
        _instances[workspace.id] = instance
        asyncio.create_task(_reap(instance))  # noqa: RUF006
        print(f"[editor] started {workspace.id} on {sock}", flush=True)
        return instance


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Signal the process group, and don't take no for an answer."""
    if proc.returncode is not None:
        return
    pid = proc.pid
    # A signal aimed at a pgid of 0 or 1 would hit the whole container instead of one editor.
    if pid <= 1:
        raise ValueError(f"refusing to signal pid {pid!r}: not an editor process")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


async def stop(workspace_id: str) -> bool:
    """Stop a workspace's editor. Returns whether there was one to stop."""
    instance = _instances.pop(workspace_id, None)
    if instance is None:
        return False
    if instance.http is not None:
        await instance.http.aclose()
        instance.http = None
    await _terminate(instance.proc)
    instance.socket_path.unlink(missing_ok=True)
    return True


def forget_workspace(workspace_id: str) -> None:
    """Drop the editor state belonging to a deleted workspace.

    Its user-data-dir holds that workspace's editor layout, open editors and per-extension state,
    all of it keyed to a folder that is about to stop existing. Leaving it would also hand it to
    the next workspace that happens to reuse the name. The shared settings are untouched.
    """
    shutil.rmtree(user_data_dir(workspace_id), ignore_errors=True)
    _locks.pop(workspace_id, None)


async def stop_all() -> None:
    for workspace_id in list(_instances):
        await stop(workspace_id)
