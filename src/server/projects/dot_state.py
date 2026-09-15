import attr

from server.projects import git_status
from server.projects.git_status import WorkspaceStatus
from server.pull_requests import PullRequest

# What the dot in the sidebar says, which is not "what git thinks" but "how far this work is from
# done". Everything short of a pull request is something still owed; a PR is the work handed over.
UNTOUCHED = "untouched"  # nothing of its own: clean, synced, no commits past its base
UNCOMMITTED = "uncommitted"  # changed or untracked files
UNPUSHED = "unpushed"  # committed, not pushed
UNREVIEWED = "unreviewed"  # pushed and synced, but nobody has been asked to look
PR_OPEN = "pr_open"  # open or draft -- both mean "it's out there"
PR_MERGED = "pr_merged"
PR_CLOSED = "pr_closed"

# Straight from git, and they outrank everything: a workspace that is broken, half-cloned or
# mid-merge has a more urgent story than where its work sits in the pipeline.
CONFLICTED = git_status.CONFLICTED
CLONING = git_status.CLONING
UNAVAILABLE = git_status.UNAVAILABLE

_PR_STATES = {
    "open": PR_OPEN,
    "draft": PR_OPEN,
    "merged": PR_MERGED,
    "closed": PR_CLOSED,
}


@attr.s(auto_attribs=True, frozen=True)
class WorkspaceView:
    """One row of the sidebar: the dot it draws, and everything the hover card explains it with."""

    workspace_id: str
    dot: str
    git: WorkspaceStatus
    pull_request: PullRequest | None = None


def dot_state(status: WorkspaceStatus, pull_request: PullRequest | None) -> str:
    """Fold a workspace's git state and its pull request into the one thing the dot shows.

    Order matters, and it is the order of what you would have to do next: fix the merge, commit,
    push, open a PR. A PR only colours the dot once the work is actually in it -- uncommitted or
    unpushed changes mean what's under review isn't what's on disk, and the dot says so.
    """
    if status.state in (CLONING, UNAVAILABLE, CONFLICTED):
        return status.state
    if status.changed or status.untracked:
        return UNCOMMITTED
    if status.ahead:
        return UNPUSHED
    if pull_request is not None:
        return _PR_STATES.get(pull_request.state, UNREVIEWED)
    # Clean, synced, and nobody to review: whether that is "nothing here yet" or "this needs a PR"
    # is the difference between a workspace still sitting on its base branch and one carrying work.
    # Only a counted zero is "nothing here". None means the base couldn't be worked out, and a
    # workspace we can't measure should read as work owed rather than as one to stop looking at.
    return UNTOUCHED if status.own_commits == 0 else UNREVIEWED
