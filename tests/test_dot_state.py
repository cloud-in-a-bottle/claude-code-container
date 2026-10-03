from __future__ import annotations

import pytest

from server.projects import dot_state
from server.projects.git_status import WorkspaceStatus
from server.pull_requests import PullRequest


def status(state: str = "clean", **fields: object) -> WorkspaceStatus:
    return WorkspaceStatus(workspace_id="proj/ws", state=state, **fields)  # type: ignore[arg-type]


def pull_request(state: str) -> PullRequest:
    return PullRequest(number=26, branch="zack/dots", state=state, title="Dots", url="https://x/26")


# ── nothing owed ───────────────────────────────────────────────────────────────


def test_a_workspace_with_nothing_of_its_own_is_untouched() -> None:
    """Clean, pushed, and still sitting on the branch it was made from: nothing to do here."""
    assert dot_state.dot_state(status(own_commits=0), None) == dot_state.UNTOUCHED


def test_work_nobody_has_been_asked_to_review_is_unreviewed() -> None:
    assert dot_state.dot_state(status(own_commits=3), None) == dot_state.UNREVIEWED


def test_a_workspace_whose_base_cannot_be_found_is_not_called_untouched() -> None:
    """`own_commits` is None when there was no origin/HEAD to measure against. "Can't tell" must
    not read as "nothing here" -- the quiet state is the one you'd never look at again."""
    assert dot_state.dot_state(status(own_commits=None), None) == dot_state.UNREVIEWED


# ── work still owed ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fields", [{"changed": 2}, {"untracked": 1}, {"changed": 1, "untracked": 1}])
def test_anything_uncommitted_is_uncommitted(fields: dict[str, int]) -> None:
    assert dot_state.dot_state(status("dirty", **fields), None) == dot_state.UNCOMMITTED


def test_commits_that_have_not_been_pushed_are_unpushed() -> None:
    assert dot_state.dot_state(status(ahead=2, own_commits=2), None) == dot_state.UNPUSHED


# ── handed over ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state, dot", [("open", dot_state.PR_OPEN), ("draft", dot_state.PR_OPEN)])
def test_draft_and_open_read_the_same(state: str, dot: str) -> None:
    """Both mean the work is out there; the card says which."""
    assert dot_state.dot_state(status(own_commits=1), pull_request(state)) == dot


def test_merged_and_closed_get_their_own_colours() -> None:
    assert dot_state.dot_state(status(own_commits=1), pull_request("merged")) == dot_state.PR_MERGED
    assert dot_state.dot_state(status(own_commits=1), pull_request("closed")) == dot_state.PR_CLOSED


def test_uncommitted_work_outranks_the_pull_request() -> None:
    """What's under review isn't what's on disk, and the dot has to say the more urgent one."""
    assert dot_state.dot_state(status("dirty", changed=1), pull_request("open")) == dot_state.UNCOMMITTED


def test_unpushed_commits_outrank_the_pull_request() -> None:
    assert dot_state.dot_state(status(ahead=1), pull_request("open")) == dot_state.UNPUSHED


def test_being_behind_the_remote_does_not_change_the_dot() -> None:
    """`↓2` in the row already says it, and nothing is owed by you for being behind."""
    assert dot_state.dot_state(status(behind=2, own_commits=1), pull_request("open")) == dot_state.PR_OPEN


# ── git's own trouble comes first ──────────────────────────────────────────────


@pytest.mark.parametrize("state", [dot_state.CONFLICTED, dot_state.CLONING, dot_state.UNAVAILABLE])
def test_a_broken_workspace_says_so_before_anything_else(state: str) -> None:
    assert dot_state.dot_state(status(state, conflicted=1), pull_request("merged")) == state
