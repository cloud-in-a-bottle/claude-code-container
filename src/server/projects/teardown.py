from server import billing
from server.editor import instances as editor_instances
from server.projects.archive_store import drop_archived
from server.projects.workspaces import Workspace
from server.projects.workspaces import delete_workspace
from server.tab_store import PersistedTab
from server.tabs import _tabs
from server.tabs import kill_tab
from server.tabs import persist_tabs
from server.tabs import persisted_tabs_for_workspace
from server.tabs import tabs_for_workspace


async def stop_workspace_processes(workspace: Workspace) -> tuple[PersistedTab, ...]:
    """Close everything a workspace is running, and return what its terminals were.

    Shared by deleting a workspace and by archiving one, because the order matters and would
    otherwise be written twice: the tabs have to be read before they are killed (their cwds come
    off the live processes), and dropping them from the registry before killing them is what keeps
    the next startup from restoring tabs pointing at nothing.

    The editor is stopped but its saved state is left alone — that belongs to the workspace
    directory, which this doesn't touch.
    """
    running = persisted_tabs_for_workspace(workspace.id)
    for tab in tabs_for_workspace(workspace.id):
        _tabs.pop(tab.id, None)
        kill_tab(tab)
    # Said here rather than left to kill_tab's own write: "the saved tab list no longer mentions
    # this workspace" is the guarantee both callers depend on -- it is what lets the next startup
    # restore stay ignorant of the archive -- and it should not rest on a side effect inside
    # something that may not even be called, for a workspace whose terminals were all closed
    # already.
    persist_tabs()
    await editor_instances.stop(workspace.id)
    return running


async def teardown_workspace(workspace: Workspace) -> None:
    """Kill a workspace's terminals and editor, then delete its directory. Not recoverable.

    An editor left running over a deleted directory holds a GB of process for a folder that no
    longer exists, which is why the stopping happens first.
    """
    await stop_workspace_processes(workspace)
    editor_instances.forget_workspace(workspace.id)
    # Dropped here rather than in the route, so the files don't keep a row for every workspace an
    # automated run has been and gone with.
    billing.forget_workspace(workspace.id)
    drop_archived(workspace.id)
    delete_workspace(workspace)
