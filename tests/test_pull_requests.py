from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any

import pytest

from server import pull_requests
from server.projects.store import Project

NODES = [
    {"number": 43, "headRefName": "zack/open", "state": "OPEN", "isDraft": False, "title": "Open", "url": "u/43"},
    {"number": 42, "headRefName": "zack/draft", "state": "OPEN", "isDraft": True, "title": "Draft", "url": "u/42"},
    {"number": 41, "headRefName": "zack/merged", "state": "MERGED", "isDraft": False, "title": "M", "url": "u/41"},
    {"number": 40, "headRefName": "zack/closed", "state": "CLOSED", "isDraft": False, "title": "C", "url": "u/40"},
]


@pytest.fixture(autouse=True)
def clear_snapshots() -> Generator[None]:
    pull_requests._snapshots.clear()
    yield
    pull_requests._snapshots.clear()


# ── reading GitHub's answer ────────────────────────────────────────────────────


def test_states_map_onto_the_dot_vocabulary() -> None:
    by_branch = pull_requests.parse_nodes(NODES)
    assert {b: pr.state for b, pr in by_branch.items()} == {
        "zack/open": "open",
        "zack/draft": "draft",  # an open PR marked draft is a draft, not an open one
        "zack/merged": "merged",
        "zack/closed": "closed",
    }
    assert by_branch["zack/open"].number == 43


def test_the_newest_pull_request_wins_a_reused_branch() -> None:
    """A branch can be opened, closed and opened again; the query is newest-first, so is this."""
    nodes = [
        {"number": 9, "headRefName": "zack/again", "state": "OPEN", "isDraft": False, "title": "now", "url": "u/9"},
        {"number": 4, "headRefName": "zack/again", "state": "CLOSED", "isDraft": False, "title": "then", "url": "u/4"},
    ]
    assert pull_requests.parse_nodes(nodes)["zack/again"].number == 9


def test_nodes_that_make_no_sense_are_skipped() -> None:
    nodes = [
        {"number": 1, "headRefName": "", "state": "OPEN"},
        {"number": 2, "headRefName": "b", "state": "ELATED"},
        {"number": 3, "headRefName": "c", "state": "OPEN", "isDraft": False, "title": "t", "url": "u"},
    ]
    assert list(pull_requests.parse_nodes(nodes)) == ["c"]


# ── which repo a project is ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/cloud-in-a-bottle/claude-code-container.git",
        "https://github.com/cloud-in-a-bottle/claude-code-container",
        "git@github.com:cloud-in-a-bottle/claude-code-container.git",
        "ssh://git@github.com/cloud-in-a-bottle/claude-code-container.git",
    ],
)
def test_the_owner_and_name_come_out_of_any_github_url(url: str) -> None:
    assert pull_requests.repo_slug(url) == ("cloud-in-a-bottle", "claude-code-container")


@pytest.mark.parametrize("url", ["https://gitlab.com/o/r.git", "https://example.com/o/r.git", "not a url"])
def test_a_repo_that_is_not_on_github_has_no_pull_requests_to_ask_about(url: str) -> None:
    assert pull_requests.repo_slug(url) is None


# ── the snapshot ───────────────────────────────────────────────────────────────


def _stub_fetch(monkeypatch: pytest.MonkeyPatch, answers: list[dict[str, Any] | None]) -> list[str]:
    """Answer fetch_repo from a script, and log which repos were asked about."""
    asked: list[str] = []

    async def fake(repo_url: str, token: str) -> dict[str, Any] | None:
        asked.append(repo_url)
        return answers[len(asked) - 1]

    monkeypatch.setattr(pull_requests, "fetch_repo", fake)

    async def fake_token() -> str:
        return "token"

    monkeypatch.setattr(pull_requests, "fetch_github_token", fake_token)
    return asked


def test_a_branch_finds_its_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_fetch(monkeypatch, [pull_requests.parse_nodes(NODES)])
    project = Project(id="p", name="p", repo_url="https://github.com/o/r.git")
    asyncio.run(pull_requests.refresh((project,)))

    found = pull_requests.pull_request_for(project.repo_url, "zack/merged")
    assert found is not None and found.state == "merged"
    assert pull_requests.pull_request_for(project.repo_url, "zack/nothing") is None
    assert pull_requests.pull_request_for("https://github.com/o/other.git", "zack/merged") is None


def test_a_failed_fetch_keeps_the_last_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rate limit shouldn't turn every reviewed workspace back into an unreviewed one."""
    _stub_fetch(monkeypatch, [pull_requests.parse_nodes(NODES), None])
    project = Project(id="p", name="p", repo_url="https://github.com/o/r.git")
    asyncio.run(pull_requests.refresh((project,)))
    asyncio.run(pull_requests.refresh((project,)))

    assert pull_requests.pull_request_for(project.repo_url, "zack/open") is not None


def test_two_projects_on_one_repo_are_asked_about_once(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _stub_fetch(monkeypatch, [pull_requests.parse_nodes(NODES)])
    projects = (
        Project(id="a", name="a", repo_url="https://github.com/o/r.git"),
        Project(id="b", name="b", repo_url="https://github.com/o/r.git"),
    )
    asyncio.run(pull_requests.refresh(projects))
    assert asked == ["https://github.com/o/r.git"]
