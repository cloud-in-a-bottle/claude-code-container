import asyncio
import os
import re
import subprocess
import time
from pathlib import Path

import attr

from server.projects.workspaces import Workspace

# What a workspace's dot can say. Anything that isn't a healthy checkout gets a state of its own
# rather than an empty status, so the UI can explain *why* it has nothing to show.
CLEAN = "clean"
DIRTY = "dirty"
CONFLICTED = "conflicted"
CLONING = "cloning"
UNAVAILABLE = "unavailable"

# These run against repos Claude is working in right now, so every call has to be read-only and
# unattended: GIT_OPTIONAL_LOCKS keeps a status poll from taking the index lock out from under a
# `git commit` in a terminal, and GIT_TERMINAL_PROMPT keeps one from ever waiting on stdin.
_GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}

_TIMEOUT_SECONDS = 10.0
# One core, shared with the Claude sessions that are the point of the workbench: a sidebar poll
# must not put more than a few `git` processes on it at once, however many workspaces there are.
_MAX_CONCURRENT = 4
# Long enough that a second browser tab, or a reload, is free; short enough that a commit made in a
# terminal reaches the sidebar about as soon as the poll comes round for it.
_CACHE_TTL_SECONDS = 3.0

# `git log` fields, separated by a unit separator. The subject is last and split off with a maxsplit
# so a commit message containing that byte can't shift the fields around it.
_LOG_FORMAT = "%h%x1f%cr%x1f%s"
_LOG_SEPARATOR = "\x1f"

_INSERTIONS_RE = re.compile(r"(\d+) insertion")
_DELETIONS_RE = re.compile(r"(\d+) deletion")

_cache: dict[str, tuple[float, WorkspaceStatus]] = {}


@attr.s(auto_attribs=True, frozen=True)
class Counts:
    """`git status --porcelain=v2 --branch`, counted up."""

    branch: str = ""  # empty when HEAD is detached
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    changed: int = 0  # tracked files that differ from HEAD, staged or not
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    conflicted: int = 0


@attr.s(auto_attribs=True, frozen=True)
class WorkspaceStatus:
    workspace_id: str
    state: str
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    changed: int = 0
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    conflicted: int = 0
    insertions: int = 0
    deletions: int = 0
    head: str = ""  # short sha of HEAD
    subject: str = ""
    committed: str = ""  # how long ago HEAD was committed, e.g. "2 hours ago"
    # Why the state is what it is, when that isn't obvious: git's own error, or what it's waiting on.
    detail: str = ""


async def run_git(cwd: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_GIT_ENV
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        return 124, "", "timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def parse_counts(porcelain: str) -> Counts:
    """Read the v2 porcelain: `# branch.*` headers, then one line per changed or untracked path."""
    branch = upstream = ""
    ahead = behind = changed = staged = unstaged = untracked = conflicted = 0

    for line in porcelain.splitlines():
        if line.startswith("# branch.head "):
            head = line.removeprefix("# branch.head ").strip()
            branch = "" if head == "(detached)" else head
        elif line.startswith("# branch.upstream "):
            upstream = line.removeprefix("# branch.upstream ").strip()
        elif line.startswith("# branch.ab "):
            for field in line.removeprefix("# branch.ab ").split():
                if field.startswith("+"):
                    ahead = int(field[1:])
                elif field.startswith("-"):
                    behind = int(field[1:])
        elif line.startswith(("1 ", "2 ")):
            # `<1|2> <XY> ...`: X is the staged change, Y the unstaged one, `.` for neither.
            changed += 1
            xy = line.split(" ", 2)[1]
            if xy[0] != ".":
                staged += 1
            if xy[1] != ".":
                unstaged += 1
        elif line.startswith("u "):
            conflicted += 1
        elif line.startswith("? "):
            untracked += 1

    return Counts(
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        changed=changed,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        conflicted=conflicted,
    )


def parse_shortstat(shortstat: str) -> tuple[int, int]:
    """The insertions and deletions out of ` 3 files changed, 42 insertions(+), 7 deletions(-)`.

    Either half is missing when it's zero, and both are when nothing changed.
    """
    insertions = _INSERTIONS_RE.search(shortstat)
    deletions = _DELETIONS_RE.search(shortstat)
    return (int(insertions.group(1)) if insertions else 0, int(deletions.group(1)) if deletions else 0)


def first_line(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


async def read_status(workspace: Workspace) -> WorkspaceStatus:
    """Ask git what state one workspace is in. Never raises for a repo it can't read — an
    unreadable workspace is a status too, and one the sidebar has to be able to show."""
    if not (workspace.path / ".git").exists():
        # The workspace directory is made before the clone that fills it, and the clone runs in a
        # terminal the user is watching, so a workspace without a repo is normally still arriving.
        return WorkspaceStatus(workspace_id=workspace.id, state=CLONING, detail="not a git repository yet")

    rc, porcelain, err = await run_git(workspace.path, "status", "--porcelain=v2", "--branch")
    if rc != 0:
        return WorkspaceStatus(workspace_id=workspace.id, state=UNAVAILABLE, detail=first_line(err))
    counts = parse_counts(porcelain)

    rc, log, _err = await run_git(workspace.path, "log", "-1", f"--format={_LOG_FORMAT}")
    if rc != 0:
        # No commits to describe. `git clone` creates .git first and checks out last, so this is
        # the same story as the missing .git above: the workspace is still being built.
        return WorkspaceStatus(workspace_id=workspace.id, state=CLONING, branch=counts.branch, detail="no commits yet")
    head, committed, subject = log.rstrip("\n").split(_LOG_SEPARATOR, 2)

    # Staged and unstaged together, which is what "how far is this workspace from its last commit"
    # means to someone reading the sidebar. Untracked files are counted, not diffed, as ever.
    _rc, shortstat, _err = await run_git(workspace.path, "diff", "--shortstat", "HEAD")
    insertions, deletions = parse_shortstat(shortstat)

    if counts.conflicted:
        state = CONFLICTED
    elif counts.changed or counts.untracked:
        state = DIRTY
    else:
        state = CLEAN

    return WorkspaceStatus(
        workspace_id=workspace.id,
        state=state,
        branch=counts.branch,
        upstream=counts.upstream,
        ahead=counts.ahead,
        behind=counts.behind,
        changed=counts.changed,
        staged=counts.staged,
        unstaged=counts.unstaged,
        untracked=counts.untracked,
        conflicted=counts.conflicted,
        insertions=insertions,
        deletions=deletions,
        head=head,
        subject=subject,
        committed=committed,
    )


async def read_statuses(workspaces: tuple[Workspace, ...]) -> tuple[WorkspaceStatus, ...]:
    """Statuses for a whole sidebar's worth of workspaces, a few `git` processes at a time."""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    started = time.monotonic()

    async def one(workspace: Workspace) -> WorkspaceStatus:
        cached = _cache.get(workspace.id)
        if cached is not None and started - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        async with semaphore:
            status = await read_status(workspace)
        _cache[workspace.id] = (time.monotonic(), status)
        return status

    statuses = await asyncio.gather(*(one(w) for w in workspaces))
    # A deleted workspace would otherwise keep its last status here for the life of the process.
    live = {w.id for w in workspaces}
    for gone in [workspace_id for workspace_id in _cache if workspace_id not in live]:
        del _cache[gone]
    return tuple(statuses)
