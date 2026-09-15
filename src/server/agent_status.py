import json
import time
from pathlib import Path

import attr

from server.config import STATE_DIR
from server.projects.workspaces import Workspace

# Where claude-home/agent_status_hook.py leaves one file per Claude session. The workbench doesn't
# run the agents -- they're `claude` in a PTY, free to start, stop and be killed without telling
# anyone -- so their state arrives from the agents themselves rather than being tracked here.
AGENT_STATUS_DIR = STATE_DIR / "agent-status"

WORKING = "working"  # a prompt is in flight
WAITING = "waiting"  # Claude has asked for something and nobody has answered
IDLE = "idle"  # a session sitting at its prompt with the last turn finished

# Which state a workspace takes when it has several agents: whichever most wants a human. An agent
# waiting on you outranks one that is busy, because only one of them is blocked on you.
_RANK = (WAITING, WORKING, IDLE)

# How long a report outlives the session that wrote it. SessionEnd deletes the file, but a killed
# agent never gets to send one, so a report whose session no tab is still running expires instead
# of leaving a dot that will never change again.
STALE_AFTER_SECONDS = 30 * 60
# When the file itself is swept up. Long enough to survive a workbench restart with the sidebar
# intact; short enough that the directory is never more than a day's sessions.
KEEP_FOR_SECONDS = 24 * 60 * 60


@attr.s(auto_attribs=True, frozen=True)
class AgentReport:
    """One session's last reported state, as its hook wrote it."""

    session_id: str
    state: str
    cwd: str
    at: float
    message: str = ""


@attr.s(auto_attribs=True, frozen=True)
class WorkspaceAgentStatus:
    workspace_id: str
    state: str
    # When the newest report behind this state was written, as a unix timestamp. The card turns it
    # into "working for 2 min", which is the part anyone reads.
    since: float
    # How many sessions are in this workspace at all, so the card can say "2 agents" rather than
    # implying the one it is describing is all there is.
    agents: int
    # The last thing the agent said, when it has finished saying it.
    message: str = ""


def prune_reports(older_than: float) -> None:
    """Delete reports last written before `older_than`, a unix timestamp."""
    if not AGENT_STATUS_DIR.is_dir():
        return
    for path in AGENT_STATUS_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime < older_than:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def read_reports() -> tuple[AgentReport, ...]:
    """Every report on disk, newest first.

    Anything unreadable is skipped rather than raised: these files are written by a hook running in
    someone else's process, so a truncated or half-renamed one is a thing that can happen, and the
    sidebar has to keep working when it does.
    """
    if not AGENT_STATUS_DIR.is_dir():
        return ()

    reports: list[AgentReport] = []
    for path in AGENT_STATUS_DIR.glob("*.json"):
        try:
            raw = json.loads(path.read_text())
        except OSError, ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        state = str(raw.get("state", ""))
        if state not in _RANK:
            continue
        reports.append(
            AgentReport(
                session_id=str(raw.get("session_id", path.stem)),
                state=state,
                cwd=str(raw.get("cwd", "")),
                at=float(raw.get("at", 0.0)),
                message=str(raw.get("message", "")),
            )
        )
    return tuple(sorted(reports, key=lambda r: r.at, reverse=True))


def is_current(report: AgentReport, live_sessions: frozenset[str], now: float) -> bool:
    """Whether a report still describes something real.

    A session the workbench is still running a tab for is believed however old its last report is --
    an agent left thinking overnight is still thinking. Everything else has to be recent, which is
    what stops a killed agent from being "working" forever.
    """
    if report.session_id in live_sessions:
        return True
    return now - report.at < STALE_AFTER_SECONDS


def workspace_for(cwd: str, workspaces: tuple[Workspace, ...]) -> Workspace | None:
    """The workspace an agent running in `cwd` belongs to, or None if it isn't in one.

    Matched by path rather than by tab, so a `claude` someone started by hand in a shell -- with a
    session id the workbench has never heard of -- still lights up the right row.
    """
    if not cwd:
        return None
    path = Path(cwd)
    return next((w for w in workspaces if path == w.path or w.path in path.parents), None)


def statuses_for(
    workspaces: tuple[Workspace, ...], live_sessions: frozenset[str], now: float | None = None
) -> tuple[WorkspaceAgentStatus, ...]:
    """Fold every current report into at most one status per workspace."""
    now = time.time() if now is None else now
    by_workspace: dict[str, list[AgentReport]] = {}
    for report in read_reports():
        if not is_current(report, live_sessions, now):
            continue
        workspace = workspace_for(report.cwd, workspaces)
        if workspace is not None:
            by_workspace.setdefault(workspace.id, []).append(report)

    statuses: list[WorkspaceAgentStatus] = []
    for workspace_id, reports in by_workspace.items():
        # read_reports() is newest first, so the first report of the winning state is also the
        # most recent one in it -- which is the age worth showing.
        leading = min(reports, key=lambda r: _RANK.index(r.state))
        statuses.append(
            WorkspaceAgentStatus(
                workspace_id=workspace_id,
                state=leading.state,
                since=leading.at,
                agents=len(reports),
                message=leading.message,
            )
        )
    return tuple(sorted(statuses, key=lambda s: s.workspace_id))
