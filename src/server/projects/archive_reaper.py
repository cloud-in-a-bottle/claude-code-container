import asyncio
import time

from server.projects.archive_store import drop_archived
from server.projects.archive_store import load_archive
from server.projects.retention import NEVER
from server.projects.retention import load_retention_days
from server.projects.teardown import teardown_workspace
from server.projects.workspaces import parse_workspace_id

# Retention is counted in days, so checking more often than this buys nothing.
REAP_INTERVAL_SECONDS = 60 * 60

_SECONDS_PER_DAY = 24 * 60 * 60


async def reap_expired_archives(now: float | None = None) -> None:
    """Delete every workspace that has been archived for longer than the retention setting allows.

    An entry with no archive time is skipped rather than treated as archived at the epoch: that
    would make it the first thing deleted, on the strength of a field that is merely missing.
    """
    days = load_retention_days()
    if days == NEVER:
        return
    cutoff = (time.time() if now is None else now) - days * _SECONDS_PER_DAY
    for workspace_id, record in load_archive().items():
        workspace = parse_workspace_id(workspace_id)
        if workspace is None or not workspace.path.is_dir():
            # Deleted by hand while archived; the entry is all that's left of it.
            drop_archived(workspace_id)
            continue
        if record.archived_at <= 0 or record.archived_at > cutoff:
            continue
        print(f"[archive] {workspace_id} has been archived for over {days} day(s); deleting it", flush=True)
        await teardown_workspace(workspace)


async def reap_archives_periodically(interval_seconds: float = REAP_INTERVAL_SECONDS) -> None:
    """Run the reaper for the life of the process.

    Never lets an exception end the loop: this is a background task, so a raise here would stop
    reaping silently for the rest of the run and nobody would notice until the disk filled up.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await reap_expired_archives()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[archive] reaper failed: {e}", flush=True)
