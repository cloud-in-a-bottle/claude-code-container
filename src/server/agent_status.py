import json
import os
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
# How long "working" is believed with nothing behind it. A turn reports when it starts and when it
# ends, and nothing fires in between -- nor when you interrupt one, which is the case this exists
# for. Claude Code appends to the transcript at every tool call, so a turn that is really running
# keeps proving it; past this, one that isn't settles back to idle instead of pulsing forever.
# Long enough to cover a single slow tool call, short enough that an interrupted turn lets go.
WORKING_GRACE_SECONDS = 120.0

# What Claude Code writes into a session's transcript when a turn is interrupted. Nothing fires a
# hook for that -- confirmed against 2.1.288, where escape leaves the session reporting "working"
# indefinitely -- so this marker is the one prompt signal there is. Like everything else that reads
# a transcript (see server.claude_sessions), it is an unpublished format: not finding it reads as
# "no", which leaves the grace window above to catch the interrupt a few seconds later instead.
INTERRUPTED_MARKER = "[Request interrupted by user]"
# How much of the end of a transcript to read looking for it. Entries run to a few KB, and a
# conversation of any age runs to megabytes, so only the tail is ever touched.
TRANSCRIPT_TAIL_BYTES = 16 * 1024


@attr.s(auto_attribs=True, frozen=True)
class AgentReport:
    """One session's last reported state, as its hook wrote it."""

    session_id: str
    state: str
    cwd: str
    at: float
    message: str = ""
    # The session's transcript, as the hook was told it. Empty for reports written before this was
    # recorded, which simply means the report has to stand on its own age.
    transcript: str = ""


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


def entry_text(entry: dict[str, object]) -> str | None:
    """The words in one transcript entry, or None if it isn't the kind that has any.

    A transcript carries plenty of bookkeeping between the messages -- modes, titles, file
    snapshots -- and those say nothing about whether a turn is still running.
    """
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return None


def turn_was_interrupted(transcript: str) -> bool:
    """Whether the last thing said in this session was someone pressing escape.

    Read from the end of the file, newest entry first: the first entry that carries any words
    decides. Anything unexpected -- no file, a partial line, a format that has moved on -- reads as
    "no", because the cost of a wrong "yes" is a dot that lies about a working agent.
    """
    if not transcript:
        return False
    try:
        with open(transcript, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TRANSCRIPT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return False

    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the first line of the tail is usually half an entry; a last one can be too
        if not isinstance(entry, dict):
            continue
        text = entry_text(entry)
        if text is not None:
            return INTERRUPTED_MARKER in text
    return False


def last_sign_of_life(report: AgentReport) -> float:
    """The most recent moment this session is known to have done anything."""
    if not report.transcript:
        return report.at
    try:
        return max(report.at, Path(report.transcript).stat().st_mtime)
    except OSError:
        return report.at


def settled(report: AgentReport, now: float) -> AgentReport:
    """Read a `working` report with nothing behind it any more as what it really is.

    The hooks say when a turn starts and when it ends. Nothing fires when one is *interrupted*, so
    the sidebar would otherwise pulse away at a session that stopped working the moment someone
    pressed escape -- and, because the workbench still has that session's tab, forever.

    Two ways out, in order of how fast they notice: the interrupt Claude Code records in the
    transcript, and failing that, a working report that nothing has backed up for a while.
    """
    if report.state != WORKING:
        return report
    # The interrupt itself, which lands in the transcript the moment escape is pressed and so
    # clears the dot on the next poll rather than at the end of the grace window.
    if turn_was_interrupted(report.transcript):
        return attr.evolve(report, state=IDLE)
    if now - last_sign_of_life(report) < WORKING_GRACE_SECONDS:
        return report
    return attr.evolve(report, state=IDLE)


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
        report = settled(report, now)
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
