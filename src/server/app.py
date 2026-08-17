import asyncio
import shutil
import traceback

import hypercorn.asyncio
import hypercorn.config
from quart import Quart
from quart import Response
from quart import jsonify
from quart import redirect
from quart import render_template
from quart import request
from quart.typing import ResponseReturnValue

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

app = Quart(__name__, template_folder=str(APP_DIR / "templates"), static_folder=str(APP_DIR / "static"))


@app.after_request
async def no_cache_static(response: Response) -> Response:
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/health")
async def health() -> ResponseReturnValue:
    return {"status": "ok"}, 200


def _settings_json(settings: UiSettings) -> dict[str, object]:
    return {"side_panel": settings.side_panel, "theme": settings.theme}


@app.get("/")
async def index() -> ResponseReturnValue:
    settings = load_ui_settings()
    html = await render_template("index.html", side_panel_enabled=settings.side_panel, theme=settings.theme)
    return html, 200, {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/api/ui/settings")
async def get_ui_settings() -> ResponseReturnValue:
    return jsonify(_settings_json(load_ui_settings()))


@app.post("/api/ui/settings")
async def update_ui_settings() -> ResponseReturnValue:
    """Update UI settings. Keys left out keep their current value.

    `side_panel` takes effect on the next page load; `theme` is applied live by the client.
    """
    data = await request.get_json(silent=True) or {}
    known = {"side_panel", "theme"}
    if not known & data.keys():
        return jsonify({"error": f"expected at least one of: {', '.join(sorted(known))}"}), 400

    current = load_ui_settings()
    theme = str(data.get("theme", current.theme))
    if theme not in THEMES:
        return jsonify({"error": f"unknown theme {theme!r}; expected one of: {', '.join(THEMES)}"}), 400

    settings = UiSettings(
        side_panel=bool(data.get("side_panel", current.side_panel)),
        theme=theme,
    )
    save_ui_settings(settings)
    return jsonify(_settings_json(settings))


@app.get("/api/tabs")
async def list_tabs() -> ResponseReturnValue:
    result = []
    for t in _tabs.values():
        program, cwd = tab_proc_info(t)
        result.append(
            {"id": t.id, "label": t.label, "connected": t.connected, "alive": t.alive, "program": program, "cwd": cwd}
        )
    return jsonify(result)


@app.post("/api/tabs")
async def create_tab() -> ResponseReturnValue:
    data = await request.get_json(silent=True) or {}
    label: str | None = (data.get("label") or "").strip() or None
    tab = await new_bash_tab(label=label)
    return jsonify({"id": tab.id, "label": tab.label})


@app.get("/github-repo")
async def open_github_repo() -> ResponseReturnValue:
    """Clone a GitHub repo and open it in Claude Code with the openhost-context skill loaded.

    GET /github-repo?repo=https://github.com/user/repo
    """
    repo = (request.args.get("repo") or "").strip()
    if not repo:
        return jsonify({"error": "repo is required"}), 400
    if not validate_repo_url(repo):
        return jsonify({"error": "invalid repo URL"}), 400

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
    return redirect(f"/?tab={tab.id}", code=303)


@app.post("/api/tabs/<tab_id>/kick")
async def kick_tab_client(tab_id: str) -> ResponseReturnValue:
    tab = _tabs.get(tab_id)
    if tab is None:
        return jsonify({"error": "not_found"}), 404
    try:
        await kick_tab(tab)
    except TimeoutError:
        return jsonify({"error": "timeout"}), 504
    return jsonify({"ok": True})


@app.delete("/api/tabs/<tab_id>")
async def delete_tab(tab_id: str) -> ResponseReturnValue:
    tab = _tabs.pop(tab_id, None)
    if tab is None:
        return jsonify({"error": "not_found"}), 404
    kill_tab(tab)
    return jsonify({"ok": True})


@app.post("/api/sessions")
async def create_session() -> ResponseReturnValue:
    """Reserve a prefilled Claude session."""
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
    return jsonify({"id": tab.id, "url": f"/?tab={tab.id}"})


async def _read_repo_ref() -> tuple[str, str]:
    repo = ""
    ref = ""
    try:
        form = await request.form
        repo = (form.get("repo") or "").strip()
        ref = (form.get("ref") or "").strip()
    except Exception:
        pass
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
async def open_workspace() -> ResponseReturnValue:
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
    repo, ref = await _read_repo_ref()

    if not repo:
        return jsonify({"error": "bad_request", "message": "repo is required"}), 400
    if not validate_repo_url(repo):
        return jsonify({"error": "bad_request", "message": "repo must be an http(s)/ssh/git@ clone url"}), 400
    if not ref:
        return jsonify({"error": "bad_request", "message": "ref is required"}), 400
    if not REF_RE.match(ref):
        return jsonify({"error": "bad_request", "message": "ref contains invalid characters"}), 400

    access = await resolve_access(repo, ref)
    if access.decision == "forbidden":
        return jsonify({"error": "access_denied", "message": "no authorization to access this repository"}), 403
    if access.decision == "not_found":
        return jsonify({"error": "not_found", "message": "repository or ref not found"}), 404
    if access.decision == "error":
        return jsonify({"error": "internal_error", "message": "could not reach the repository"}), 500

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
    return redirect(f"/?tab={tab.id}", code=303)


@app.websocket("/terminal/ws")
async def terminal_ws() -> None:
    try:
        await handle_terminal_ws()
    except Exception:
        traceback.print_exc()
        raise


async def _serve() -> None:
    await seed_oh_config()
    await seed_gh_auth()
    # Bring back the tabs from the previous run before any client connects, so the first page
    # load already shows them instead of racing to create a fresh one.
    await restore_tabs()
    asyncio.create_task(persist_tabs_periodically())

    cfg = hypercorn.config.Config()
    cfg.bind = [f"0.0.0.0:{PORT}"]
    cfg.accesslog = "-"
    await hypercorn.asyncio.serve(app, cfg)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
