import urllib.parse
from typing import Any

from litestar import Request
from litestar import Response
from litestar import route
from litestar.enums import HttpMethod
from litestar.response import Redirect

from server.git_remote import REF_RE
from server.git_remote import repo_dir_name
from server.git_remote import resolve_access
from server.git_remote import validate_repo_url
from server.projects.launch import start_workspace_tab
from server.projects.store import add_project
from server.projects.store import find_project_by_repo
from server.projects.workspaces import Workspace
from server.projects.workspaces import create_workspace_dir
from server.projects.workspaces import unique_workspace_name
from server.routes.common import JsonDict
from server.routes.common import error
from server.routes.common import json_body


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
        data = await json_body(request)
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

    Given a `repo` clone URL and a `ref`, register the repo as a project if it isn't one already,
    create a fresh workspace at that commit, and 303-redirect the user into a terminal sitting in
    it. Inputs may arrive as form fields, a JSON body, or query params; both are required.

    Each call gets its own workspace rather than reusing one, so a second visit can never disturb
    work in progress from the first. Workspaces are cheap: they clone from the project's local
    mirror.

    The contract is POST-only, but we also accept GET as a workaround for the openhost router's
    login bounce: an unauthenticated POST gets `302`'d to `/login?next=…`, and a browser following
    that demotes the eventual return hop to GET (per HTTP/1.1: only 307/308 preserve method).
    Accepting GET means the post-login landing still resolves instead of 405-ing. Once the router
    switches to 307/308 we can drop GET here.
    """
    repo, ref = await _read_repo_ref(request)

    if not repo:
        return error(400, error="bad_request", message="repo is required")
    if not validate_repo_url(repo):
        return error(400, error="bad_request", message="repo must be an http(s)/ssh/git@ clone url")
    if not ref:
        return error(400, error="bad_request", message="ref is required")
    if not REF_RE.match(ref):
        return error(400, error="bad_request", message="ref contains invalid characters")

    access = await resolve_access(repo, ref)
    if access.decision == "forbidden":
        return error(403, error="access_denied", message="no authorization to access this repository")
    if access.decision == "not_found":
        return error(404, error="not_found", message="repository or ref not found")
    if access.decision == "error":
        return error(500, error="internal_error", message="could not reach the repository")

    project = find_project_by_repo(repo) or add_project(name=repo_dir_name(repo), repo_url=repo)
    workspace = Workspace(project_id=project.id, name=unique_workspace_name(project.id, ref))
    create_workspace_dir(workspace)
    tab = await start_workspace_tab(project, workspace, ref=ref, github_token=access.token)

    return Redirect(
        f"/?workspace={urllib.parse.quote(workspace.id)}&tab={urllib.parse.quote(tab.id)}", status_code=303
    )
