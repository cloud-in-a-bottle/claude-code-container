"""Which workspaces are archived, and what was running in them when they were.

An archived workspace still exists on disk exactly as it was -- this file only records that it has
been put away, plus the terminals that were killed to put it away, so unarchiving can bring the
same Claude conversations back. That makes it a per-workspace attribute like billing.json, not a
second source of truth for which workspaces exist: the workspace list is still read off disk.

Nothing on the startup path reads this, and that is deliberate -- see the ordering notes in
archive.py, which is what keeps a crash mid-archive from needing this file to be understood.
"""

import json
import time
from collections.abc import Mapping

import attr

from server.config import STATE_DIR
from server.tab_store import PersistedTab
from server.tab_store import tab_from_json
from server.tab_store import tab_to_json

# Under $HOME, which openhost points at the app's persistent data dir, so an archived workspace is
# still archived after a redeploy, alongside the workspace directory it describes.
ARCHIVE_PATH = STATE_DIR / "archive.json"


@attr.s(auto_attribs=True, frozen=True)
class ArchivedWorkspace:
    workspace_id: str
    archived_at: float
    # The terminals that were running when it was archived, in the form a restore needs. This is
    # the only copy of them: archiving kills the tabs, which takes them out of tabs.json.
    tabs: tuple[PersistedTab, ...] = ()


def load_archive() -> Mapping[str, ArchivedWorkspace]:
    """The archived workspaces, keyed by workspace id.

    A malformed file raises rather than reporting an empty archive. Guessing here would present
    every archived workspace as a live one, which is how you end up with Claude restarted in a
    workspace you had deliberately put away.
    """
    if not ARCHIVE_PATH.exists():
        return {}
    raw = json.loads(ARCHIVE_PATH.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"malformed archive at {ARCHIVE_PATH}: expected an object")

    archived: dict[str, ArchivedWorkspace] = {}
    for workspace_id, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"malformed archive at {ARCHIVE_PATH}: entry for {workspace_id} is not an object")
        saved = entry.get("tabs", [])
        if not isinstance(saved, list):
            raise ValueError(f"malformed archive at {ARCHIVE_PATH}: tabs for {workspace_id} must be a list")
        tabs = tuple(tab for tab in (tab_from_json(t) for t in saved) if tab is not None)
        archived[str(workspace_id)] = ArchivedWorkspace(
            workspace_id=str(workspace_id),
            archived_at=float(entry.get("archived_at", 0.0)),
            tabs=tabs,
        )
    return archived


def save_archive(archive: Mapping[str, ArchivedWorkspace]) -> None:
    """Persist via a temp file + rename, so an interrupted write can't corrupt the archive."""
    ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        workspace_id: {
            "archived_at": record.archived_at,
            "tabs": [tab_to_json(t) for t in record.tabs],
        }
        for workspace_id, record in archive.items()
    }
    tmp_path = ARCHIVE_PATH.with_name(ARCHIVE_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
    tmp_path.replace(ARCHIVE_PATH)


def archived_record(workspace_id: str) -> ArchivedWorkspace | None:
    return load_archive().get(workspace_id)


def is_archived(workspace_id: str) -> bool:
    return workspace_id in load_archive()


def put_archived(workspace_id: str, tabs: tuple[PersistedTab, ...]) -> ArchivedWorkspace:
    record = ArchivedWorkspace(workspace_id=workspace_id, archived_at=time.time(), tabs=tabs)
    save_archive({**load_archive(), workspace_id: record})
    return record


def drop_archived(workspace_id: str) -> None:
    """Forget a workspace's archive entry — it has been unarchived, or deleted outright."""
    archive = load_archive()
    if workspace_id not in archive:
        return
    save_archive({k: v for k, v in archive.items() if k != workspace_id})
