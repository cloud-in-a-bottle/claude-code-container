import asyncio
import shutil
import traceback
from typing import Any
from typing import cast

import hypercorn.asyncio
import hypercorn.config
from litestar import Litestar
from litestar import MediaType
from litestar import Request
from litestar import Response
from litestar import WebSocket
from litestar import delete
from litestar import get
from litestar import post
from litestar import route
from litestar import websocket
from litestar.datastructures import CacheControlHeader
from litestar.enums import HttpMethod
from litestar.params import FromPath
from litestar.params import FromQuery
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.response import Redirect
from litestar.response import Template
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig

from server.config import APP_DIR
from server.config import HOME
from server.config import OPENHOST_DIR
from server.config import PORT
from server.remote_services import get_anthropic_key
from server.remote_services import seed_gh_auth
from server.remote_services import seed_oh_config
from server.tab_store import CLAUDE
from server.tab_store import SHELL
from server.tabs import _tabs
from server.tabs import create_server_tab
from server.tabs import handle_terminal_ws
from server.tabs import kick_tab
from server.tabs import kill_tab
from server.tabs import new_bash_tab
from server.tabs import new_session_id
from server.tabs import persist_tabs_periodically
from server.tabs import restore_tabs
from server.tabs import set_active_cwd
from server.tabs import tab_proc_info
from server.ui_settings import THEMES
from server.ui_settings import UiSettings
from server.ui_settings import load_ui_settings
from server.ui_settings import save_ui_settings
from server.workspace import REF_RE
from server.workspace import WORKSPACE_SCRIPT
from server.workspace import repo_dir_name
from server.workspace import resolve_access
from server.workspace import validate_repo_url

GITHUB_REPO_SCRIPT = APP_DIR / "github_repo.sh"

NO_CACHE = CacheControlHeader(no_cache=True, no_store=True, must_revalidate=True)

type JsonDict = dict[str, Any]


def _error(status: int, **body: Any) -> Response[JsonDict]:
    return Response(content=body, status_code=status)


async def _json_body(request: Request[Any, Any, Any]) -> JsonDict:
    """The request's JSON object, or {} when there isn't a usable one.

    Callers here treat a missing or malformed body the same as an empty one and produce their own
    error, rather than letting the framework reject it with a shape the clients don't expect.
    """
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@get("/health", sync_to_thread=False)
def health() -> JsonDict:
    return {"status": "ok"}


def _settings_json(settings: UiSettings) -> JsonDict:
    return {"side_panel": settings.side_panel, "theme": settings.theme}


@get("/", media_type=MediaType.HTML, cache_control=NO_CACHE, sync_to_thread=False)
def index() -> Template:
    settings = load_ui_settings()
    return Template(
        template_name="index.html",
        context={"side_panel_enabled": settings.side_panel, "theme": settings.theme},
    )


@get("/api/ui/settings", sync_to_thread=False)
def get_ui_settings() -> JsonDict:
    return _settings_json(load_ui_settings())


@post("/api/ui/settings", status_code=200)
async def update_ui_settings(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Update UI settings. Keys left out keep their current value.

    `side_panel` takes effect on the next page load; `theme` is applied live by the client.
    """
    data = await _json_body(request)
    known = {"side_panel", "theme"}
    if not known & data.keys():
        return _error(400, error=f"expected at least one of: {', '.join(sorted(known))}")

    current = load_ui_settings()
    theme = str(data.get("theme", current.theme))
    if theme not in THEMES:
        return _error(400, error=f"unknown theme {theme!r}; expected one of: {', '.join(THEMES)}")

    settings = UiSettings(side_panel=bool(data.get("side_panel", current.side_panel)), theme=theme)
    save_ui_settings(settings)
    return Response(content=_settings_json(settings))


@get("/api/tabs", sync_to_thread=False)
def list_tabs() -> list[JsonDict]:
    result: list[JsonDict] = []
    for t in _tabs.values():
        program, cwd = tab_proc_info(t)
        result.append(
            {"id": t.id, "label": t.label, "connected": t.connected, "alive": t.alive, "program": program, "cwd": cwd}
        )
    return result


@post("/api/tabs", status_code=200)
async def create_tab(request: Request[Any, Any, Any]) -> JsonDict:
    data = await _json_body(request)
    label: str | None = str(data.get("label") or "").strip() or None
    tab = await new_bash_tab(label=label)
    return {"id": tab.id, "label": tab.label}


@get("/github-repo")
async def open_github_repo(repo: FromQuery[str] = "") -> Response[JsonDict] | Redirect:
    """Clone a GitHub repo and open it in Claude Code with the openhost-context skill loaded.

    GET /github-repo?repo=https://github.com/user/repo
    """
    repo = repo.strip()
    if not repo:
        return _error(400, error="repo is required")
    if not validate_repo_url(repo):
        return _error(400, error="invalid repo URL")

    repo_name = repo_dir_name(repo)
    dest = HOME / repo_name

    # Update the working directory for all tabs (existing + future) and claim the Claude slot.
    set_active_cwd(str(dest))

    key = await get_anthropic_key()
    session_id = new_session_id()
    env: dict[str, str] = {
        "GITHUB_REPO": repo,
        "GITHUB_DIR": str(dest),
        "CLAUDE_BIN": shutil.which("claude") or "claude",
        "CLAUDE_SESSION_ID": session_id,
    }
    if key:
        env["ANTHROPIC_API_KEY"] = key

    tab = await create_server_tab(
        command=["bash", "-l", str(GITHUB_REPO_SCRIPT)],
        cwd=str(HOME),
        env=env,
        label=repo_name,
        # The script clones and then hands over to Claude; a restore must re-enter that
        # conversation in the checkout, never re-run the clone.
        kind=CLAUDE,
        session_id=session_id,
    )
    return Redirect(f"/?tab={tab.id}", status_code=303)


@post("/api/tabs/{tab_id:str}/kick", status_code=200)
async def kick_tab_client(tab_id: FromPath[str]) -> Response[JsonDict]:
    tab = _tabs.get(tab_id)
    if tab is None:
        return _error(404, error="not_found")
    try:
        await kick_tab(tab)
    except TimeoutError:
        return _error(504, error="timeout")
    return Response(content={"ok": True})


@delete("/api/tabs/{tab_id:str}", status_code=200)
async def delete_tab(tab_id: FromPath[str]) -> Response[JsonDict]:
    tab = _tabs.pop(tab_id, None)
    if tab is None:
        return _error(404, error="not_found")
    kill_tab(tab)
    return Response(content={"ok": True})


@post("/api/sessions", status_code=200)
async def create_session(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Reserve a prefilled Claude session."""
    data = await _json_body(request)
    prompt = str(data.get("prompt") or "").strip()
    context = str(data.get("context") or "").strip()
    if not prompt and not context:
        return _error(400, error="prompt or context required")

    seed_parts: list[str] = []
    if context:
        seed_parts.append(f"# Context\n\n{context}\n")
    if prompt:
        seed_parts.append(prompt)
    seed = "\n\n".join(seed_parts) + "\n"

    key = await get_anthropic_key()
    env: dict[str, str] = {}
    if key:
        env["ANTHROPIC_API_KEY"] = key

    session_id = new_session_id()
    tab = await create_server_tab(
        command=["claude", "--session-id", session_id, "--dangerously-skip-permissions"],
        stdin_seed=seed,
        cwd=str(OPENHOST_DIR if OPENHOST_DIR.exists() else HOME),
        env=env,
        label="claude",
        kind=CLAUDE,
        session_id=session_id,
    )
    return Response(content={"id": tab.id, "url": f"/?tab={tab.id}"})


async def _read_repo_ref(request: Request[Any, Any, Any]) -> tuple[str, str]:
    repo = ""
    ref = ""
    try:
        form = await request.form()
        repo = str(form.get("repo") or "").strip()
        ref = str(form.get("ref") or "").strip()
    except Exception:
        pass
    if not (repo and ref):
        data = await _json_body(request)
        repo = repo or str(data.get("repo") or "").strip()
        ref = ref or str(data.get("ref") or "").strip()
    if not repo:
        repo = (request.query_params.get("repo") or "").strip()
    if not ref:
        ref = (request.query_params.get("ref") or "").strip()
    return repo, ref


@route("/open-workspace", http_method=[HttpMethod.GET, HttpMethod.POST], status_code=200)
async def open_workspace(request: Request[Any, Any, Any]) -> Response[JsonDict] | Redirect:
    """Provider for the open-workspace service (services/open-workspace/openapi.yaml).

    Given a `repo` clone URL and a `ref`, prepare a checkout of that repo at that commit and
    303-redirect the user into a terminal sitting in it. Inputs may arrive as form fields, a JSON
    body, or query params; both are required.

    The contract is POST-only, but we also accept GET as a workaround for the openhost router's
    login bounce: an unauthenticated POST gets `302`'d to `/login?next=…`, and a browser following
    that demotes the eventual return hop to GET (per HTTP/1.1: only 307/308 preserve method).
    Accepting GET means the post-login landing still resolves instead of 405-ing. Once the router
    switches to 307/308 we can drop GET here.
    """
    repo, ref = await _read_repo_ref(request)

    if not repo:
        return _error(400, error="bad_request", message="repo is required")
    if not validate_repo_url(repo):
        return _error(400, error="bad_request", message="repo must be an http(s)/ssh/git@ clone url")
    if not ref:
        return _error(400, error="bad_request", message="ref is required")
    if not REF_RE.match(ref):
        return _error(400, error="bad_request", message="ref contains invalid characters")

    access = await resolve_access(repo, ref)
    if access.decision == "forbidden":
        return _error(403, error="access_denied", message="no authorization to access this repository")
    if access.decision == "not_found":
        return _error(404, error="not_found", message="repository or ref not found")
    if access.decision == "error":
        return _error(500, error="internal_error", message="could not reach the repository")

    env: dict[str, str] = {
        "WORKSPACE_REPO": repo,
        "WORKSPACE_DIR": repo_dir_name(repo),
        "WORKSPACE_REF": ref,
    }
    if access.token:
        env["WORKSPACE_GITHUB_TOKEN"] = access.token

    tab = await create_server_tab(
        command=["bash", "-l", str(WORKSPACE_SCRIPT)],
        cwd=str(HOME),
        env=env,
        label=repo_dir_name(repo),
        # The script clones and then execs a plain shell, so that is what a restore recreates.
        kind=SHELL,
    )
    return Redirect(f"/?tab={tab.id}", status_code=303)


@websocket("/terminal/ws")
async def terminal_ws(socket: WebSocket[Any, Any, Any]) -> None:
    try:
        await handle_terminal_ws(socket)
    except Exception:
        traceback.print_exc()
        raise


async def _on_startup() -> None:
    await seed_oh_config()
    await seed_gh_auth()
    # Bring back the tabs from the previous run before any client connects, so the first page
    # load already shows them instead of racing to create a fresh one.
    await restore_tabs()
    asyncio.create_task(persist_tabs_periodically())  # noqa: RUF006


app = Litestar(
    route_handlers=[
        health,
        index,
        get_ui_settings,
        update_ui_settings,
        list_tabs,
        create_tab,
        open_github_repo,
        kick_tab_client,
        delete_tab,
        create_session,
        open_workspace,
        terminal_ws,
        create_static_files_router(path="/static", directories=[APP_DIR / "static"], cache_control=NO_CACHE),
    ],
    template_config=TemplateConfig(directory=APP_DIR / "templates", engine=JinjaTemplateEngine),
    on_startup=[_on_startup],
)


def main() -> None:
    cfg = hypercorn.config.Config()
    cfg.bind = [f"0.0.0.0:{PORT}"]
    cfg.accesslog = "-"
    # hypercorn describes ASGI apps with its own Scope TypedDicts; Litestar's are structurally
    # equivalent but nominally distinct, so the two annotations can't be reconciled.
    asyncio.run(hypercorn.asyncio.serve(cast("Any", app), cfg))


if __name__ == "__main__":
    main()
