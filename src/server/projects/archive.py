"""Putting a workspace away, and getting it back.

Archiving closes everything a workspace is running — its terminals, the Claude sessions inside
them, its editor — and writes down what was running so unarchiving can start the same terminals
again, each Claude tab resuming the conversation it was in. The workspace directory itself is
never touched: archiving is about processes, not about work on disk.

Both operations are two steps that can be interrupted between, so each is ordered to fail towards
the same harmless state — the workspace listed as live with no terminals open, which is exactly
what a workspace whose tabs you closed by hand looks like, and which opening it recovers from on
its own. Archiving therefore kills first and records second; unarchiving drops the record first
and restores second. Neither ordering can leave a tab list that is both live and archived, which
is why nothing on the startup path has to read the archive to know what to skip.
"""

from server.projects.archive_store import ArchivedWorkspace
from server.projects.archive_store import archived_record
from server.projects.archive_store import drop_archived
from server.projects.archive_store import put_archived
from server.projects.teardown import stop_workspace_processes
from server.projects.workspaces import Workspace
from server.tabs import ServerTab
from server.tabs import restore_persisted_tabs


async def archive_workspace(workspace: Workspace) -> ArchivedWorkspace:
    """Close the workspace down and file it away. The directory is left exactly as it is."""
    tabs = await stop_workspace_processes(workspace)
    record = put_archived(workspace.id, tabs)
    print(f"[archive] archived {workspace.id} ({len(tabs)} terminal(s) closed)", flush=True)
    return record


async def unarchive_workspace(workspace: Workspace) -> list[ServerTab]:
    """Bring an archived workspace back, reopening the terminals it was archived with.

    Returns the restored tabs, which may be fewer than were archived — a tab whose directory has
    since been deleted is dropped rather than reopened somewhere arbitrary. A workspace that isn't
    archived comes back with no tabs rather than an error: it is already in the state asked for.
    """
    record = archived_record(workspace.id)
    if record is None:
        return []
    drop_archived(workspace.id)
    restored = await restore_persisted_tabs(record.tabs)
    print(f"[archive] unarchived {workspace.id} ({len(restored)} terminal(s) reopened)", flush=True)
    return restored
