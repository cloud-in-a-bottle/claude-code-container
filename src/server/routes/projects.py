import shutil
from typing import Any

from litestar import Request
from litestar import Response
from litestar import delete
from litestar import get
from litestar import patch
from litestar import post
from litestar.params import FromPath

from server.git_remote import REF_RE
from server.git_remote import RepoAccess
from server.git_remote import repo_dir_name
from server.git_remote import resolve_access
from server.git_remote import validate_repo_url
from server.projects.launch import start_workspace_tab
from server.projects.store import Project
from server.projects.store import add_project
from server.projects.store import find_project
from server.projects.store import load_projects
from server.projects.store import remove_project
from server.projects.store import save_projects
from server.projects.workspaces import Workspace
from server.projects.workspaces import create_workspace_dir
from server.projects.workspaces import delete_workspace
from server.projects.workspaces import list_workspaces
from server.projects.workspaces import mirror_path
from server.projects.workspaces import parse_workspace_id
from server.projects.workspaces import unique_workspace_name
from server.routes.common import JsonDict
from server.routes.common import error
from server.routes.common import json_body
from server.routes.tabs import tab_json
from server.tabs import _tabs
from server.tabs import kill_tab
from server.tabs import tabs_for_workspace

# What the caller sees when a repo can't be reached, keyed by RepoAccess.decision.
_ACCESS_ERRORS = {
    "forbidden": (403, "access_denied", "no authorization to access this repository"),
    "not_found": (404, "not_found", "repository or ref not found"),
    "error": (500, "internal_error", "could not reach the repository"),
}


def project_json(project: Project) -> JsonDict:
    return {
        "id": project.id,
        "name": project.name,
        "repo_url": project.repo_url,
        "setup": project.setup,
        "workspaces": [{"id": w.id, "name": w.name, "path": str(w.path)} for w in list_workspaces(project.id)],
    }


def _access_error(access: RepoAccess) -> Response[JsonDict] | None:
    known = _ACCESS_ERRORS.get(access.decision)
    if known is None:
        return None
    status, code, message = known
    return error(status, error=code, message=message, detail=access.detail)


@get("/api/projects", sync_to_thread=False)
def list_projects() -> list[JsonDict]:
    return [project_json(p) for p in load_projects()]


@post("/api/projects", status_code=200)
async def create_project(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Register a git repo as a project. The name defaults to the repo's own name."""
    data = await json_body(request)
    repo_url = str(data.get("repo_url") or "").strip()
    if not repo_url:
        return error(400, error="bad_request", message="repo_url is required")
    if not validate_repo_url(repo_url):
        return error(400, error="bad_request", message="repo_url must be an http(s)/ssh/git@ clone url")

    # Check the repo is really reachable now, rather than letting every workspace creation fail
    # later with a wall of git output.
    access = await resolve_access(repo_url, "HEAD")
    failed = _access_error(access)
    if failed is not None:
        return failed

    name = str(data.get("name") or "").strip() or repo_dir_name(repo_url)
    project = add_project(name=name, repo_url=repo_url, setup=str(data.get("setup") or ""))
    return Response(content=project_json(project))


@patch("/api/projects/{project_id:str}", status_code=200)
async def update_project(project_id: FromPath[str], request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Edit a project's name or setup command. Keys left out keep their current value."""
    project = find_project(project_id)
    if project is None:
        return error(404, error="not_found", message=f"no project {project_id}")

    data = await json_body(request)
    known = {"name", "setup"}
    if not known & data.keys():
        return error(400, error="bad_request", message=f"expected at least one of: {', '.join(sorted(known))}")

    name = str(data.get("name", project.name)).strip()
    if not name:
        return error(400, error="bad_request", message="name cannot be empty")
    updated = Project(id=project.id, name=name, repo_url=project.repo_url, setup=str(data.get("setup", project.setup)))
    save_projects(tuple(updated if p.id == project.id else p for p in load_projects()))
    return Response(content=project_json(updated))


@delete("/api/projects/{project_id:str}", status_code=200)
async def delete_project(project_id: FromPath[str]) -> Response[JsonDict]:
    """Remove a project and its mirror. Its workspaces have to be deleted first — they hold work,
    and losing them to a click on the wrong row would be unrecoverable."""
    project = find_project(project_id)
    if project is None:
        return error(404, error="not_found", message=f"no project {project_id}")
    workspaces = list_workspaces(project_id)
    if workspaces:
        names = ", ".join(w.name for w in workspaces)
        return error(409, error="has_workspaces", message=f"delete its workspaces first: {names}")

    remove_project(project_id)
    shutil.rmtree(mirror_path(project_id), ignore_errors=True)
    return Response(content={"ok": True})


@post("/api/workspaces", status_code=200)
async def create_workspace(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Make a new copy of a project's repo and open it in a Claude tab.

    The directory is created here so the workspace shows up immediately; the clone itself runs in
    the tab, where its output (and the project's setup command) is something you can watch.
    """
    data = await json_body(request)
    project = find_project(str(data.get("project_id") or ""))
    if project is None:
        return error(404, error="not_found", message="no such project")

    ref = str(data.get("ref") or "").strip()
    if ref and not REF_RE.match(ref):
        return error(400, error="bad_request", message="ref contains invalid characters")

    access = await resolve_access(project.repo_url, ref or "HEAD")
    failed = _access_error(access)
    if failed is not None:
        return failed

    requested = str(data.get("name") or "").strip()
    name = unique_workspace_name(project.id, requested or ref or "workspace")
    workspace = Workspace(project_id=project.id, name=name)
    create_workspace_dir(workspace)

    tab = await start_workspace_tab(project, workspace, ref=ref, github_token=access.token)
    return Response(
        content={"id": workspace.id, "name": workspace.name, "project_id": project.id, "tab": tab_json(tab)}
    )


@delete("/api/workspaces/{project_id:str}/{name:str}", status_code=200)
async def remove_workspace(project_id: FromPath[str], name: FromPath[str]) -> Response[JsonDict]:
    """Delete a workspace: kill its terminals, then delete the directory. This is not recoverable."""
    # Parsed rather than trusted: a name like `..` would otherwise resolve to the project's whole
    # workspace directory, and this function deletes what it is given.
    workspace = parse_workspace_id(f"{project_id}/{name}")
    if workspace is None:
        return error(400, error="bad_request", message="invalid workspace id")
    if not workspace.path.is_dir():
        return error(404, error="not_found", message=f"no workspace {project_id}/{name}")

    for tab in tabs_for_workspace(workspace.id):
        _tabs.pop(tab.id, None)
        kill_tab(tab)
    delete_workspace(workspace)
    return Response(content={"ok": True})
