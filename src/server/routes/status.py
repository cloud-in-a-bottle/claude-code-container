from litestar import get

from server.projects.dot_state import WorkspaceView
from server.projects.dot_state import dot_state
from server.projects.git_status import read_statuses
from server.projects.store import Project
from server.projects.store import load_projects
from server.projects.workspaces import Workspace
from server.projects.workspaces import list_workspaces
from server.pull_requests import pull_request_for


@get("/api/workspaces/status")
async def workspace_status() -> tuple[WorkspaceView, ...]:
    """Every workspace's dot, and what the hover card explains it with.

    One call for the whole sidebar rather than one per row: the client polls this while the page is
    visible, and a request per workspace would put that many `git` processes on the container's
    single core every time round. The pull request half never touches the network here -- it comes
    from a snapshot a background task keeps current, so GitHub being slow can't hold up git.
    """
    owners: dict[str, Project] = {}
    workspaces: list[Workspace] = []
    for project in load_projects():
        for workspace in list_workspaces(project.id):
            owners[workspace.id] = project
            workspaces.append(workspace)

    views: list[WorkspaceView] = []
    for status in await read_statuses(tuple(workspaces)):
        pull_request = pull_request_for(owners[status.workspace_id].repo_url, status.branch)
        views.append(
            WorkspaceView(
                workspace_id=status.workspace_id,
                dot=dot_state(status, pull_request),
                git=status,
                pull_request=pull_request,
            )
        )
    return tuple(views)
