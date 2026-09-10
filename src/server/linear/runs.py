import json
from datetime import UTC
from datetime import datetime

import attr

from server.config import STATE_DIR

# Under $HOME like every other piece of workbench state, so tracked runs survive a redeploy and
# a merged PR still gets its workspace cleaned up afterwards.
RUNS_PATH = STATE_DIR / "linear-runs.json"


@attr.s(auto_attribs=True, frozen=True)
class LinearRun:
    """One issue being worked on in one workspace.

    This is the only thing the workbench remembers about a run, and it exists for exactly one
    reason: the reaper has to know which PR belongs to which workspace so it can delete the
    workspace once that PR merges. Workspaces themselves stay discovered from disk.
    """

    workspace_id: str
    issue_id: str
    issue_identifier: str
    issue_url: str
    # `owner/name`, the form `gh --repo` takes.
    repo: str
    branch: str
    created_at: str


def load_runs() -> tuple[LinearRun, ...]:
    """Read the tracked runs, or () when there is no usable file.

    Never raises: this is read from a background loop and from a webhook handler, and a corrupt
    file should cost us the ability to auto-delete merged workspaces, not the ability to start new
    work. The workspaces themselves are still on disk and still deletable by hand.
    """
    if not RUNS_PATH.exists():
        return ()
    try:
        raw = json.loads(RUNS_PATH.read_text())
    except (OSError, ValueError) as e:
        print(f"[linear] ignoring unreadable run list at {RUNS_PATH}: {e}", flush=True)
        return ()
    if not isinstance(raw, list):
        print(f"[linear] ignoring malformed run list at {RUNS_PATH}: expected a list", flush=True)
        return ()

    runs: list[LinearRun] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("workspace_id"):
            continue
        runs.append(
            LinearRun(
                workspace_id=str(entry["workspace_id"]),
                issue_id=str(entry.get("issue_id", "")),
                issue_identifier=str(entry.get("issue_identifier", "")),
                issue_url=str(entry.get("issue_url", "")),
                repo=str(entry.get("repo", "")),
                branch=str(entry.get("branch", "")),
                created_at=str(entry.get("created_at", "")),
            )
        )
    return tuple(runs)


def save_runs(runs: tuple[LinearRun, ...]) -> None:
    """Persist via a temp file + rename, so an interrupted write can't corrupt the list."""
    RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = [attr.asdict(r) for r in runs]
    tmp_path = RUNS_PATH.with_name(RUNS_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
    tmp_path.replace(RUNS_PATH)


def add_run(run: LinearRun) -> None:
    save_runs((*(r for r in load_runs() if r.workspace_id != run.workspace_id), run))


def remove_run(workspace_id: str) -> None:
    save_runs(tuple(r for r in load_runs() if r.workspace_id != workspace_id))


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
