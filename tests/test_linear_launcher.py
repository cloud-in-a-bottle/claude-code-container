from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from server import git_remote
from server.linear import api
from server.linear import launcher
from server.linear.api import LinearIssue
from server.linear.events import CommentEvent
from server.linear.launcher import NotForUs
from server.linear.launcher import handle_comment
from server.linear.repos import GithubRepo
from server.linear.repos import OrgRepos
from server.linear.resolve import RepoChoice
from server.linear.runs import load_runs
from server.projects import launch
from server.projects import store
from server.projects.workspaces import list_workspaces
from server.tabs import ServerTab
from server.tabs import _tabs

OWNER = "user-owner"

REPOS = OrgRepos(
    org="cloud-in-a-bottle",
    repos=(
        GithubRepo(name="md-notes", description="notes", is_private=False),
        GithubRepo(name="backup", description="backups", is_private=False),
    ),
)

ISSUE = LinearIssue(
    id="issue-1",
    identifier="ENG-7",
    title="Notes editor drops a character",
    description="Typing fast loses a keystroke.",
    labels=("notes",),
    url="https://linear.app/x/issue/ENG-7",
    team_key="ENG",
    comments=(),
)


def _event(body: str = "@claude fix it", comment_id: str = "comment-1", author: str = OWNER) -> CommentEvent:
    return CommentEvent(
        comment_id=comment_id,
        body=body,
        author_id=author,
        issue_id=ISSUE.id,
        issue_identifier=ISSUE.identifier,
        issue_title=ISSUE.title,
        delivery_timestamp_ms=0,
    )


@pytest.fixture(autouse=True)
def _wiring(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Stand in for everything outside the process: Linear, GitHub, the LLM, and the pty."""
    launcher._HANDLED_COMMENTS.clear()
    _tabs.clear()
    monkeypatch.setenv("OPENHOST_APP_NAME", "claude-workbench")
    monkeypatch.setenv("OPENHOST_ZONE_DOMAIN", "zone.example.com")

    async def viewer_id() -> str:
        return OWNER

    async def fetch_issue(issue_id: str) -> LinearIssue:
        return ISSUE

    async def list_org_repos(org: str) -> OrgRepos:
        return REPOS

    async def resolve_access(repo: str, ref: str) -> git_remote.RepoAccess:
        return git_remote.RepoAccess(decision="ok", token="tok")

    monkeypatch.setattr(api, "viewer_id", viewer_id)
    monkeypatch.setattr(api, "fetch_issue", fetch_issue)
    monkeypatch.setattr(launcher, "list_org_repos", list_org_repos)
    monkeypatch.setattr(launcher, "resolve_access", resolve_access)
    yield
    _tabs.clear()


@pytest.fixture
def comments(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    posted: list[str] = []

    async def post_comment(issue_id: str, body: str) -> None:
        posted.append(body)

    monkeypatch.setattr(api, "post_comment", post_comment)
    return posted


@pytest.fixture
def tabs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> ServerTab:
        tab = ServerTab(
            id=f"tab-{len(created)}",
            label="claude",
            master_fd=-1,
            proc=MagicMock(),
            workspace_id=kwargs.get("workspace_id", ""),
        )
        _tabs[tab.id] = tab
        created.append(kwargs)
        return tab

    monkeypatch.setattr(launch, "create_server_tab", fake)
    return created


def _choose(monkeypatch: pytest.MonkeyPatch, repo: str = "md-notes", confidence: float = 0.95) -> None:
    async def choose_repo(issue: LinearIssue, repos: OrgRepos) -> RepoChoice:
        return RepoChoice(repo=repo, confidence=confidence, alternatives=("backup",), reasoning="the notes label")

    monkeypatch.setattr(launcher, "choose_repo", choose_repo)


class TestWhoMayTrigger:
    def test_a_comment_from_someone_else_is_ignored(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch, tabs: list[dict[str, Any]]
    ) -> None:
        """The endpoint is public, so a comment by anyone but the owner must never start work."""
        _choose(monkeypatch)
        with pytest.raises(NotForUs):
            asyncio.run(handle_comment(_event(author="someone-else")))
        assert tabs == []

    def test_a_failure_to_identify_the_owner_stops_the_run(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch, tabs: list[dict[str, Any]]
    ) -> None:
        """ "We couldn't tell who this was" must not degrade into "run it anyway"."""
        _choose(monkeypatch)

        async def unavailable() -> str:
            raise api.LinearError("latchkey is not connected")

        monkeypatch.setattr(api, "viewer_id", unavailable)
        with pytest.raises(api.LinearError):
            asyncio.run(handle_comment(_event()))
        assert tabs == []

    def test_a_redelivered_comment_does_not_run_twice(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch, tabs: list[dict[str, Any]]
    ) -> None:
        """Linear retries a delivery it thinks timed out; the retry must not open a second run."""
        _choose(monkeypatch)
        asyncio.run(handle_comment(_event()))
        with pytest.raises(NotForUs):
            asyncio.run(handle_comment(_event()))
        assert len(tabs) == 1


class TestStartingWork:
    def test_creates_a_workspace_on_a_branch_named_for_the_issue(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch, tabs: list[dict[str, Any]]
    ) -> None:
        _choose(monkeypatch)
        asyncio.run(handle_comment(_event()))

        assert [p.repo_url for p in store.load_projects()] == ["https://github.com/cloud-in-a-bottle/md-notes.git"]
        assert [w.name for w in list_workspaces("md-notes")] == ["ENG-7"]
        assert tabs[0]["env"]["WS_BRANCH"] == "eng-7-notes-editor-drops-a-character"

    def test_the_opening_prompt_carries_the_issue_and_the_workspace_link(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch, tabs: list[dict[str, Any]]
    ) -> None:
        _choose(monkeypatch)
        asyncio.run(handle_comment(_event(body="@claude only touch the parser")))

        # Read out of the command rather than the environment: this is the text claude is
        # actually handed, quoted into the shell snippet the tab runs.
        prompt = tabs[0]["command"][3]
        assert "ENG-7: Notes editor drops a character" in prompt
        assert "only touch the parser" in prompt
        assert "https://claude-workbench.zone.example.com/?workspace=md-notes/ENG-7" in prompt
        assert "linear comment ENG-7" in prompt

    def test_records_the_run_so_the_reaper_can_find_its_pr(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch, tabs: list[dict[str, Any]]
    ) -> None:
        _choose(monkeypatch)
        asyncio.run(handle_comment(_event()))

        run = load_runs()[0]
        assert (run.workspace_id, run.repo, run.branch) == (
            "md-notes/ENG-7",
            "cloud-in-a-bottle/md-notes",
            "eng-7-notes-editor-drops-a-character",
        )

    def test_reuses_a_project_that_is_already_registered(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch, tabs: list[dict[str, Any]]
    ) -> None:
        store.add_project(name="md-notes", repo_url="https://github.com/cloud-in-a-bottle/md-notes.git")
        _choose(monkeypatch)
        asyncio.run(handle_comment(_event()))
        assert len(store.load_projects()) == 1

    def test_a_second_run_on_the_same_issue_gets_its_own_workspace(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch, tabs: list[dict[str, Any]]
    ) -> None:
        _choose(monkeypatch)
        asyncio.run(handle_comment(_event(comment_id="c1")))
        asyncio.run(handle_comment(_event(comment_id="c2")))
        assert [w.name for w in list_workspaces("md-notes")] == ["ENG-7", "ENG-7-2"]


class TestWhenItWillNotGuess:
    def test_low_confidence_asks_in_linear_instead_of_starting(
        self,
        workbench_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tabs: list[dict[str, Any]],
        comments: list[str],
    ) -> None:
        _choose(monkeypatch, confidence=0.2)
        asyncio.run(handle_comment(_event()))

        assert tabs == []
        assert load_runs() == ()
        assert "not confident which repo" in comments[0]
        assert "`md-notes`" in comments[0] and "`backup`" in comments[0]

    def test_a_failure_is_reported_on_the_issue(
        self,
        workbench_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tabs: list[dict[str, Any]],
        comments: list[str],
    ) -> None:
        """Whoever asked is waiting in Linear, not reading the app log."""

        async def unreachable(repo: str, ref: str) -> git_remote.RepoAccess:
            return git_remote.RepoAccess(decision="forbidden", detail="no access")

        _choose(monkeypatch)
        monkeypatch.setattr(launcher, "resolve_access", unreachable)
        with pytest.raises(RuntimeError):
            asyncio.run(handle_comment(_event()))

        assert tabs == []
        assert "couldn't start work" in comments[0]
