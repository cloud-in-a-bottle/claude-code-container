from litestar import get

from server.projects.git_status import WorkspaceStatus
from server.projects.git_status import read_statuses
from server.projects.store import load_projects
from server.projects.workspaces import list_workspaces


@get("/api/workspaces/status")
async def workspace_status() -> tuple[WorkspaceStatus, ...]:
    """Git status for every workspace at once.

    One call for the whole sidebar rather than one per row: the client polls this while the page is
    visible, and a request per workspace would put that many `git` processes on the container's
    single core every time round.
    """
    workspaces = tuple(w for project in load_projects() for w in list_workspaces(project.id))
    return await read_statuses(workspaces)
