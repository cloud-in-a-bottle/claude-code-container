from server import billing
from server.editor import instances as editor_instances
from server.projects.workspaces import Workspace
from server.projects.workspaces import delete_workspace
from server.tabs import _tabs
from server.tabs import kill_tab
from server.tabs import tabs_for_workspace


async def teardown_workspace(workspace: Workspace) -> None:
    """Kill a workspace's terminals and editor, then delete its directory. Not recoverable.

    The order matters and is the reason this is shared rather than written twice: an editor left
    running over a deleted directory holds a GB of process for a folder that no longer exists, and
    tabs left in the registry are restored on the next startup pointing at nothing.
    """
    for tab in tabs_for_workspace(workspace.id):
        _tabs.pop(tab.id, None)
        kill_tab(tab)
    await editor_instances.stop(workspace.id)
    editor_instances.forget_workspace(workspace.id)
    # Dropped here rather than in the route, so the file doesn't keep a billing row for every
    # workspace an automated run has been and gone with.
    billing.forget_workspace(workspace.id)
    delete_workspace(workspace)
