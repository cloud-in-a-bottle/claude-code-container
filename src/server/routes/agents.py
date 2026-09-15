import time

from litestar import get

from server.agent_status import KEEP_FOR_SECONDS
from server.agent_status import WorkspaceAgentStatus
from server.agent_status import prune_reports
from server.agent_status import statuses_for
from server.projects.store import load_projects
from server.projects.workspaces import list_workspaces
from server.tabs import _tabs


@get("/api/workspaces/agents", sync_to_thread=False)
def workspace_agents() -> tuple[WorkspaceAgentStatus, ...]:
    """What Claude is doing in each workspace, as its own sessions last reported it.

    Separate from /api/workspaces/status, and polled faster, because the two cost wildly different
    things: this reads a handful of small files, while git status runs processes. Neither should
    have to wait for the other.
    """
    workspaces = tuple(w for project in load_projects() for w in list_workspaces(project.id))
    # A session the workbench still has a running tab for is alive however quiet it has gone; see
    # is_current(). Tabs are the only part of this the workbench knows first-hand.
    live_sessions = frozenset(tab.session_id for tab in _tabs.values() if tab.alive and tab.session_id)
    # Cheap, and it keeps the directory the size of a day's sessions -- which is what keeps this
    # handler cheap, since it reads every file left in it.
    prune_reports(time.time() - KEEP_FOR_SECONDS)
    return statuses_for(workspaces, live_sessions)
