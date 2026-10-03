import shutil
from collections.abc import Collection
from typing import Any

from litestar import Request
from litestar import Response
from litestar import delete
from litestar import get
from litestar import patch
from litestar import post
from litestar.params import FromPath

from server.billing import MODES
from server.billing import Billing
from server.billing import load_billing
from server.billing import mode_of
from server.billing import pin_workspace_mode
from server.git_remote import REF_RE
from server.git_remote import RepoAccess
from server.git_remote import repo_dir_name
from server.git_remote import resolve_access
from server.git_remote import resolve_default_branch
from server.git_remote import validate_repo_url
from server.projects.archive import archive_workspace
from server.projects.archive import unarchive_workspace
from server.projects.archive_store import load_archive
from server.projects.launch import start_workspace_tab
from server.projects.store import Project
from server.projects.store import add_project
from server.projects.store import find_project
from server.projects.store import load_projects
from server.projects.store import remove_project
from server.projects.store import save_projects
from server.projects.teardown import teardown_workspace
from server.projects.workspaces import Workspace
from server.projects.workspaces import create_workspace_dir
from server.projects.workspaces import list_workspaces
from server.projects.workspaces import mirror_path
from server.projects.workspaces import parse_workspace_id
from server.projects.workspaces import unique_workspace_name
from server.routes.common import JsonDict
from server.routes.common import error
from server.routes.common import json_body
from server.routes.tabs import tab_json

# What the caller sees when a repo can't be reached, keyed by RepoAccess.decision.
_ACCESS_ERRORS = {
    "forbidden": (403, "access_denied", "no authorization to access this repository"),
    "not_found": (404, "not_found", "repository or ref not found"),
    "error": (500, "internal_error", "could not reach the repository"),
}


def workspace_json(workspace: Workspace, billing: Billing, archived: Collection[str]) -> JsonDict:
    return {
        "id": workspace.id,
        "name": workspace.name,
        "path": str(workspace.path),
        "billing": mode_of(billing, workspace.id),
        # Archived means "put away": still on disk, nothing running in it, shown apart in the rail.
        "archived": workspace.id in archived,
    }


def project_json(project: Project) -> JsonDict:
    # Read once for the whole project: a workspace's billing mode and whether it is archived each
    # come out of a single file, and this runs for every workspace of every project on each sidebar
    # refresh.
    billing = load_billing()
    archived = load_archive().keys()
    return {
        "id": project.id,
        "name": project.name,
        "repo_url": project.repo_url,
        "setup": project.setup,
        "default_branch": project.default_branch,
        "workspaces": [workspace_json(w, billing, archived) for w in list_workspaces(project.id)],
    }


def _bad_ref(value: str, field: str) -> Response[JsonDict] | None:
    """Guard every branch/ref that reaches a `git` command line."""
    if value and not REF_RE.match(value):
        return error(400, error="bad_request", message=f"{field} contains invalid characters")
    return None


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

    default_branch = str(data.get("default_branch") or "").strip()
    invalid = _bad_ref(default_branch, "default_branch")
    if invalid is not None:
        return invalid

    # Check the repo — and the branch its workspaces will start from — is really reachable now,
    # rather than letting every workspace creation fail later with a wall of git output.
    access = await resolve_access(repo_url, default_branch or "HEAD")
    failed = _access_error(access)
    if failed is not None:
        return failed

    name = str(data.get("name") or "").strip() or repo_dir_name(repo_url)
    project = add_project(
        name=name,
        repo_url=repo_url,
        setup=str(data.get("setup") or ""),
        default_branch=default_branch,
    )
    return Response(content=project_json(project))


@patch("/api/projects/{project_id:str}", status_code=200)
async def update_project(project_id: FromPath[str], request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Edit a project's name, setup command or default branch. Keys left out keep their value."""
    project = find_project(project_id)
    if project is None:
        return error(404, error="not_found", message=f"no project {project_id}")

    data = await json_body(request)
    known = {"name", "setup", "default_branch"}
    if not known & data.keys():
        return error(400, error="bad_request", message=f"expected at least one of: {', '.join(sorted(known))}")

    name = str(data.get("name", project.name)).strip()
    if not name:
        return error(400, error="bad_request", message="name cannot be empty")

    default_branch = str(data.get("default_branch", project.default_branch)).strip()
    invalid = _bad_ref(default_branch, "default_branch")
    if invalid is not None:
        return invalid
    # Only worth a round trip when it actually changed: renaming a project shouldn't wait on the
    # network, but pointing it at a branch that doesn't exist should fail here, not in a workspace.
    if default_branch and default_branch != project.default_branch:
        failed = _access_error(await resolve_access(project.repo_url, default_branch))
        if failed is not None:
            return failed

    updated = Project(
        id=project.id,
        name=name,
        repo_url=project.repo_url,
        setup=str(data.get("setup", project.setup)),
        default_branch=default_branch,
    )
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

    `billing` picks how this workspace's Claude sessions are paid for, and is pinned for the life
    of the workspace; left out, it follows the workbench default from the settings page.
    """
    data = await json_body(request)
    project = find_project(str(data.get("project_id") or ""))
    if project is None:
        return error(404, error="not_found", message="no such project")

    billing = load_billing()
    mode = str(data.get("billing") or billing.default_mode)
    if mode not in MODES:
        return error(400, error="bad_request", message=f"billing must be one of: {', '.join(MODES)}")

    ref = str(data.get("ref") or "").strip()
    invalid = _bad_ref(ref, "ref")
    if invalid is not None:
        return invalid
    # An explicit ref wins; otherwise the project's configured starting branch.
    ref = ref or project.default_branch

    access = await resolve_access(project.repo_url, ref or "HEAD")
    failed = _access_error(access)
    if failed is not None:
        return failed

    # With neither, ask the remote what its default branch is *now*, so the checkout follows a
    # renamed default instead of the one the mirror happened to see when it was first cloned.
    ref = ref or await resolve_default_branch(project.repo_url, access.token)

    requested = str(data.get("name") or "").strip()
    name = unique_workspace_name(project.id, requested or ref or "workspace")
    workspace = Workspace(project_id=project.id, name=name)
    create_workspace_dir(workspace)
    # Before the tab starts, because that is what reads it to build the tab's environment.
    pin_workspace_mode(workspace.id, mode)

    tab = await start_workspace_tab(project, workspace, ref=ref, github_token=access.token)
    return Response(
        content={
            "id": workspace.id,
            "name": workspace.name,
            "project_id": project.id,
            "billing": mode,
            "tab": tab_json(tab),
        }
    )


def _resolve_workspace(project_id: str, name: str) -> Workspace | Response[JsonDict]:
    """The workspace at `<project>/<name>`, or the error to return instead of it.

    Parsed rather than trusted: a name like `..` would otherwise resolve to the project's whole
    workspace directory, and what these routes are handed is what they close down or delete.
    """
    workspace = parse_workspace_id(f"{project_id}/{name}")
    if workspace is None:
        return error(400, error="bad_request", message="invalid workspace id")
    if not workspace.path.is_dir():
        return error(404, error="not_found", message=f"no workspace {project_id}/{name}")
    return workspace


@delete("/api/workspaces/{project_id:str}/{name:str}", status_code=200)
async def remove_workspace(project_id: FromPath[str], name: FromPath[str]) -> Response[JsonDict]:
    """Delete a workspace: kill its terminals, then delete the directory. This is not recoverable."""
    resolved = _resolve_workspace(project_id, name)
    if isinstance(resolved, Response):
        return resolved

    await teardown_workspace(resolved)
    return Response(content={"ok": True})


@post("/api/workspaces/{project_id:str}/{name:str}/archive", status_code=200)
async def archive_workspace_route(project_id: FromPath[str], name: FromPath[str]) -> Response[JsonDict]:
    """Put a workspace away: close its terminals, Claude sessions and editor, and file it under the
    project's archived section. The directory is untouched, and unarchiving reopens the terminals
    with their conversations resumed."""
    resolved = _resolve_workspace(project_id, name)
    if isinstance(resolved, Response):
        return resolved

    await archive_workspace(resolved)
    return Response(content=workspace_json(resolved, load_billing(), load_archive().keys()))


@post("/api/workspaces/{project_id:str}/{name:str}/unarchive", status_code=200)
async def unarchive_workspace_route(project_id: FromPath[str], name: FromPath[str]) -> Response[JsonDict]:
    """Bring an archived workspace back, reopening the terminals it was archived with. The response
    carries them, so the client can show them without asking again."""
    resolved = _resolve_workspace(project_id, name)
    if isinstance(resolved, Response):
        return resolved

    tabs = await unarchive_workspace(resolved)
    content = workspace_json(resolved, load_billing(), load_archive().keys())
    content["tabs"] = [tab_json(t) for t in tabs]
    return Response(content=content)
