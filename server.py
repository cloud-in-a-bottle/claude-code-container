"""claude-workbench: tabbed web terminals + Claude Code, for building/debugging openhost apps.

Routes:
    GET  /                         -> tabbed terminal UI
    GET  /health                   -> health check
    GET  /terminal/ws              -> WebSocket PTY (one session per connection)
    POST /api/sessions             -> queue a prefilled Claude session; returns {id, url}
                                      body: {"prompt": "...", "context": "..."}
                                      the next WS that connects with ?session=<id> picks it up
    POST /open-workspace           -> open-workspace service provider: clone a repo at a
    GET  /open-workspace              ref and 303-redirect into a checkout of it.
                                      body (form or json) or query: repo (required), ref (required)
                                      contract: services/open-workspace/openapi.yaml
                                      (GET is a workaround for the openhost router's
                                       login bounce demoting POST → GET; see the
                                       open_workspace() docstring.)
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import secrets
import signal
import struct
import subprocess
import termios
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import tomli_w
from quart import Quart, jsonify, redirect, request, send_from_directory, websocket

APP_DIR = Path(__file__).parent
HOME = Path(os.environ.get("HOME", "/home/workbench"))
OPENHOST_DIR = Path(os.environ.get("OPENHOST_DIR", str(HOME / "openhost")))
MY_PROJECT_DIR = HOME / "my_project"

ROUTER_URL = os.environ.get("OPENHOST_ROUTER_URL", "")
APP_TOKEN = os.environ.get("OPENHOST_APP_TOKEN", "")
SECRETS_SHORTNAME = "secrets"
OAUTH_SHORTNAME = "oauth"

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


async def _fetch_github_token() -> str:
    """Mint a `repo`-scoped GitHub token via the oauth-v2 app. "" if unavailable.

    Mirrors openhost's own clone flow (core/oauth.py `get_oauth_token`): the
    token lets us clone private repos openhost has access to. Best-effort — if
    the oauth app isn't installed, the grant is missing, or no GitHub account is
    connected, we get a non-200 and return "" so the caller falls back to an
    unauthenticated clone.
    """
    if not ROUTER_URL or not APP_TOKEN:
        return ""
    url = f"{ROUTER_URL}/api/services/v2/call/{OAUTH_SHORTNAME}/token"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={"provider": "github", "scopes": ["repo"]},
                headers={"Authorization": f"Bearer {APP_TOKEN}"},
            )
        if resp.status_code != 200:
            return ""
        return (resp.json().get("access_token") or "").strip()
    except Exception:
        return ""


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


# Sessions that have been queued by /open-workspace or /api/sessions but not yet
# attached to a websocket. Each entry is (session, expires_at_monotonic).
#
# IMPORTANT: this dict is process-local and not synchronized across workers, so
# the hypercorn config below must keep workers=1 — otherwise a POST hits one
# worker, the WS hits another, and the pending session silently falls through
# to the default `bash -l`. If we ever need to scale out, this needs to move
# behind a shared store (redis, sqlite, etc).
_pending: dict[str, tuple[PendingSession, float]] = {}
# How long a queued session waits for its websocket before we drop it. The
# normal flow attaches within a second or two; a tab closed before the redirect
# completes, a script that POSTs without following the 303, or a buggy client
# would otherwise grow this dict without bound.
_PENDING_TTL_SECONDS = 600.0


def _sweep_pending(now: float | None = None) -> None:
    """Drop expired entries from `_pending`. Called on every insert so the dict
    is bounded by concurrent in-flight sessions, not by historical traffic."""
    if now is None:
        now = asyncio.get_event_loop().time()
    expired = [sid for sid, (_, exp) in _pending.items() if exp <= now]
    for sid in expired:
        _pending.pop(sid, None)


def _put_pending(sid: str, session: PendingSession) -> None:
    """Queue a session, sweeping expired entries first."""
    now = asyncio.get_event_loop().time()
    _sweep_pending(now)
    _pending[sid] = (session, now + _PENDING_TTL_SECONDS)


def _pop_pending(sid: str | None) -> PendingSession | None:
    """Claim a queued session if it exists and hasn't expired."""
    if not sid:
        return None
    entry = _pending.pop(sid, None)
    if entry is None:
        return None
    session, expires_at = entry
    if expires_at <= asyncio.get_event_loop().time():
        return None
    return session

# A git ref/sha: no leading dash (would be read as a `git checkout` flag) and a
# conservative character set.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
# A ref that looks like a bare commit sha. `git ls-remote` only lists named
# refs, so a sha can't be validated ahead of the clone — we skip the pre-check
# for these and let the checkout degrade gracefully if the commit is missing.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# The open-workspace terminal runs this script (see open_workspace.sh). It takes
# all its inputs from env vars set on the PendingSession, so there's nothing to
# interpolate here and no shell injection surface.
_WORKSPACE_SCRIPT = APP_DIR / "open_workspace.sh"


def _repo_dir_name(url: str) -> str:
    """Derive a safe local directory name from a clone URL."""
    name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    name = re.sub(r"[^A-Za-z0-9._-]", "", name)
    return name or "repo"


# ── open-workspace access resolution ──────────────────────────────────────
#
# Before redirecting, /open-workspace probes the repo with `git ls-remote` so it
# can answer with the contract's status codes (services/open-workspace/openapi.yaml):
#   403 — repo is private and we have no authorization to reach it
#   404 — repo, or a named ref, does not exist
#   500 — network/internal failure
# A repo we can read unauthenticated needs no token; a private GitHub repo is
# retried with a freshly-minted token, which is then handed to the clone.

_NETWORK_ERR_RE = re.compile(
    r"could ?n.?t resolve host|could not resolve|failed to connect|connection (timed out|refused)"
    r"|could not connect|network is unreachable|temporary failure in name resolution",
    re.IGNORECASE,
)
_AUTH_ERR_RE = re.compile(
    r"authentication failed|could not read username|could not read password"
    r"|terminal prompts disabled|permission denied|access denied|denied to|403 forbidden",
    re.IGNORECASE,
)


@dataclass
class RepoAccess:
    """Outcome of probing a repo: a status decision plus the token (if any) the
    clone should use."""

    decision: str  # "ok" | "forbidden" | "not_found" | "error"
    token: str = ""
    detail: str = ""


def _git_host(url: str) -> str:
    """Hostname of a clone URL, handling both URL and scp-like (`git@host:path`) forms."""
    scp = re.match(r"^[A-Za-z0-9._-]+@([^:/]+):", url)
    if scp:
        return scp.group(1).lower()
    return (urllib.parse.urlparse(url).hostname or "").lower()


def _validate_repo_url(url: str) -> bool:
    """Accept http(s)/ssh URLs and scp-form (`user@host:path`) clone URLs.

    Reject anything else — notably command-capable transports like `ext::` and
    `file://`, and inputs that aren't URLs at all — so a caller can't smuggle
    in a transport that lets git execute arbitrary commands.

    Parses with `urllib`/`_git_host` rather than a single anchored regex so the
    structure (scheme, host) is what gets verified rather than just the prefix.
    """
    if not url or any(c.isspace() for c in url):
        return False
    # `_git_host` returns "" when neither the scp form nor `urllib` finds a
    # host, which rules out bare paths and malformed inputs.
    if not _git_host(url):
        return False
    # URL form: restrict the transport. scp form (no `://`) has no scheme to
    # check and is accepted as-is — git's own ssh client runs the command.
    if "://" in url:
        if urllib.parse.urlparse(url).scheme not in ("http", "https", "ssh"):
            return False
    return True


def _is_github(url: str) -> bool:
    host = _git_host(url)
    return host == "github.com" or host.endswith(".github.com")


def _inject_github_token(url: str, token: str) -> str:
    """Put a token into an http(s) URL's authority for a one-shot authenticated
    git operation. Matches openhost's `inject_github_token_in_url`. Non-http
    transports (ssh) are returned unchanged — the token can't be applied.

    The token is percent-encoded so values containing `:`/`@`/`/`/`%` don't
    corrupt the URL's authority section. GitHub tokens today are
    `[A-Za-z0-9_]` and pass through unchanged, but encoding here keeps us
    safe against future token formats and against a malformed value supplied
    by a misconfigured oauth provider."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
        encoded = urllib.parse.quote(token, safe="")
        return parsed._replace(netloc=f"{encoded}@{host}").geturl()
    return url


async def _run_ls_remote(repo: str, ref: str | None, token: str) -> tuple[int, str, str]:
    """Run `git ls-remote <repo> [ref]` with prompts disabled. Returns
    (returncode, stdout, stderr); returncode 124 signals a timeout. Inputs are
    validated before this is called and passed as argv, so there's no shell."""
    url = _inject_github_token(repo, token) if token else repo
    args = ["git", "ls-remote", url]
    if ref:
        args.append(ref)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=25)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timed out"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def _resolve_access(repo: str, ref: str) -> RepoAccess:
    """Probe `repo`/`ref`, minting a GitHub token if the repo is private."""
    # A named ref (branch/tag) can be confirmed via ls-remote; a bare sha can't,
    # so for shas we only probe repo reachability and let the checkout degrade.
    ref_probe = None if _SHA_RE.match(ref) else ref

    rc, out, err = await _run_ls_remote(repo, ref_probe, token="")
    if rc == 0:
        if ref_probe is not None and not out.strip():
            return RepoAccess("not_found", detail=f"ref {ref!r} not found")
        return RepoAccess("ok")
    if rc == 124 or _NETWORK_ERR_RE.search(err):
        return RepoAccess("error", detail=err.strip())

    # Unauthenticated read failed. For GitHub, retry with a minted token.
    if _is_github(repo):
        token = await _fetch_github_token()
        if token and _inject_github_token(repo, token) != repo:
            rc2, out2, err2 = await _run_ls_remote(repo, ref_probe, token=token)
            if rc2 == 0:
                if ref_probe is not None and not out2.strip():
                    return RepoAccess("not_found", detail=f"ref {ref!r} not found")
                return RepoAccess("ok", token=token)
            if rc2 == 124 or _NETWORK_ERR_RE.search(err2):
                return RepoAccess("error", detail=err2.strip())
            # Even with our token we can't see it: treat as not found.
            return RepoAccess("not_found", detail=err2.strip())
        # No usable token (no grant / no connected account / ssh transport):
        # the repo is private and we have no authorization for it.
        return RepoAccess("forbidden", detail=err.strip())

    # Non-GitHub host — classify from git's own error text.
    if _AUTH_ERR_RE.search(err):
        return RepoAccess("forbidden", detail=err.strip())
    return RepoAccess("not_found", detail=err.strip())


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
    _put_pending(
        sid,
        PendingSession(
            command=["claude", "--dangerously-skip-permissions"],
            stdin_seed=seed,
            cwd=str(OPENHOST_DIR if OPENHOST_DIR.exists() else HOME),
        ),
    )
    return jsonify({"id": sid, "url": f"/?session={sid}"})


async def _read_repo_ref() -> tuple[str, str]:
    """Read `repo` and `ref` from a form body, a JSON body, or the query string."""
    try:
        form = await request.form
    except Exception:
        form = {}
    repo = (form.get("repo") or "").strip()
    ref = (form.get("ref") or "").strip()
    if not (repo and ref):
        data = await request.get_json(silent=True)
        if isinstance(data, dict):
            repo = repo or str(data.get("repo") or "").strip()
            ref = ref or str(data.get("ref") or "").strip()
    if not repo:
        repo = (request.args.get("repo") or "").strip()
    if not ref:
        ref = (request.args.get("ref") or "").strip()
    return repo, ref


@app.route("/open-workspace", methods=["GET", "POST"])
async def open_workspace() -> object:
    """Provider for the open-workspace service (services/open-workspace/openapi.yaml).

    Given a `repo` clone URL and a `ref`, prepare a checkout of that repo at that
    commit and 303-redirect the user into a terminal sitting in it. Inputs may
    arrive as form fields, a JSON body, or query params; both are required.

    The contract is POST-only, but we also accept GET as a workaround for the
    openhost router's login bounce: an unauthenticated POST gets `302`'d to
    `/login?next=…`, and a browser following that demotes the eventual return
    hop to GET (per HTTP/1.1: only 307/308 preserve method). Accepting GET
    means the post-login landing still resolves instead of 405-ing. Once the
    router switches to 307/308 we can drop GET here.
    """
    repo, ref = await _read_repo_ref()

    if not repo:
        return jsonify({"error": "bad_request", "message": "repo is required"}), 400
    if not _validate_repo_url(repo):
        return jsonify({"error": "bad_request", "message": "repo must be an http(s)/ssh/git@ clone url"}), 400
    if not ref:
        return jsonify({"error": "bad_request", "message": "ref is required"}), 400
    if not _REF_RE.match(ref):
        return jsonify({"error": "bad_request", "message": "ref contains invalid characters"}), 400

    access = await _resolve_access(repo, ref)
    if access.decision == "forbidden":
        return jsonify({"error": "access_denied", "message": "no authorization to access this repository"}), 403
    if access.decision == "not_found":
        return jsonify({"error": "not_found", "message": "repository or ref not found"}), 404
    if access.decision == "error":
        return jsonify({"error": "internal_error", "message": "could not reach the repository"}), 500

    sid = secrets.token_urlsafe(8)
    env = {
        "WORKSPACE_REPO": repo,
        "WORKSPACE_DIR": _repo_dir_name(repo),
        "WORKSPACE_REF": ref,
    }
    if access.token:
        env["WORKSPACE_GITHUB_TOKEN"] = access.token
    _put_pending(
        sid,
        PendingSession(
            command=["bash", "-l", str(_WORKSPACE_SCRIPT)],
            cwd=str(HOME),
            env=env,
        ),
    )
    return redirect(f"/?session={sid}", code=303)


@app.websocket("/terminal/ws")
async def terminal_ws() -> None:
    session_id = websocket.args.get("session")
    pending = _pop_pending(session_id)

    if pending is not None:
        command = pending.command
        cwd = pending.cwd
        extra_env = dict(pending.env)
        stdin_seed = pending.stdin_seed
    else:
        command = ["bash", "-l"]
        cwd = str(MY_PROJECT_DIR) if MY_PROJECT_DIR.exists() else str(HOME)
        extra_env = {}
        stdin_seed = "claude\n"

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
    # `hypercorn.asyncio.serve` ignores `cfg.workers` and always runs a single
    # worker, which is what we need: `_pending` is in-process state shared
    # between the POST that queues a session and the WS that claims it, and
    # multiple workers would split that state across processes. If we ever
    # switch to a multi-worker entrypoint, `_pending` needs to move behind a
    # shared store (redis, sqlite, etc) first.
    await hypercorn.asyncio.serve(app, cfg)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
