import asyncio
import shutil

from quart import Quart, Response, jsonify, redirect, request, send_from_directory
from quart.typing import ResponseReturnValue

from config import APP_DIR, HOME, OPENHOST_DIR, PORT
from remote_services import get_anthropic_key, seed_gh_auth, seed_oh_config
from tabs import _tabs, create_server_tab, handle_terminal_ws, kick_tab, kill_tab, new_bash_tab, set_active_cwd
from workspace import REF_RE, WORKSPACE_SCRIPT, repo_dir_name, resolve_access, validate_repo_url

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


@app.get("/")
async def index() -> ResponseReturnValue:
    resp = await send_from_directory(str(APP_DIR / "templates"), "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/api/tabs")
async def list_tabs() -> ResponseReturnValue:
    return jsonify(
        [{"id": t.id, "label": t.label, "connected": t.connected, "alive": t.alive} for t in _tabs.values()]
    )


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
    env: dict[str, str] = {
        "GITHUB_REPO": repo,
        "GITHUB_DIR": str(dest),
        "CLAUDE_BIN": shutil.which("claude") or "claude",
    }
    if key:
        env["ANTHROPIC_API_KEY"] = key

    tab = await create_server_tab(
        command=["bash", "-l", str(GITHUB_REPO_SCRIPT)],
        cwd=str(HOME),
        env=env,
        label=repo_name,
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

    tab = await create_server_tab(
        command=["claude", "--dangerously-skip-permissions"],
        stdin_seed=seed,
        cwd=str(OPENHOST_DIR if OPENHOST_DIR.exists() else HOME),
        env=env,
        label="claude",
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
    )
    return redirect(f"/?tab={tab.id}", code=303)


@app.websocket("/terminal/ws")
async def terminal_ws() -> None:
    try:
        await handle_terminal_ws()
    except Exception:
        import traceback

        traceback.print_exc()
        raise


async def _serve() -> None:
    import hypercorn.asyncio
    import hypercorn.config

    await seed_oh_config()
    await seed_gh_auth()

    cfg = hypercorn.config.Config()
    cfg.bind = [f"0.0.0.0:{PORT}"]
    cfg.accesslog = "-"
    await hypercorn.asyncio.serve(app, cfg)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
