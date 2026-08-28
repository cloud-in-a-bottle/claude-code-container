from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Coroutine
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from server import app as srv
from server.projects import git_status
from server.projects import store
from server.projects.workspaces import Workspace

# The tests run inside a live workbench, whose $HOME has a real git identity and config. These
# repos get their own, so what they assert about is only ever what the test itself did.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


@pytest.fixture(autouse=True)
def clear_status_cache() -> Generator[None]:
    """Statuses are cached for a few seconds by workspace id, and ids repeat across tests."""
    git_status._cache.clear()
    yield
    git_status._cache.clear()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=_GIT_ENV, check=True, capture_output=True, text=True).stdout


def make_repo(path: Path, content: str = "hello\n") -> None:
    """A repo with one commit on `main`."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    (path / "file.txt").write_text(content)
    git(path, "add", "file.txt")
    git(path, "commit", "-m", "first commit")


def workspace_at(workbench_home: Path, project: str = "proj", name: str = "ws") -> Workspace:
    """A workspace whose directory is where the server will look for it."""
    workspace = Workspace(project_id=project, name=name)
    workspace.path.parent.mkdir(parents=True, exist_ok=True)
    assert workspace.path.is_relative_to(workbench_home)
    return workspace


# ── parsing ────────────────────────────────────────────────────────────────────


PORCELAIN = """\
# branch.oid 1c2d3e4
# branch.head zack/dev
# branch.upstream origin/zack/dev
# branch.ab +2 -1
1 .M N... 100644 100644 100644 abc def ui/src/store.js
1 M. N... 100644 100644 100644 abc def README.md
1 MM N... 100644 100644 100644 abc def src/server/app.py
2 R. N... 100644 100644 100644 abc def R100 new.py\told.py
u UU N... 100644 100644 100644 100644 abc def ghi conflicted.py
? untracked.txt
? another.txt
"""


def test_parse_counts_reads_the_branch_headers() -> None:
    counts = git_status.parse_counts(PORCELAIN)
    assert (counts.branch, counts.upstream) == ("zack/dev", "origin/zack/dev")
    assert (counts.ahead, counts.behind) == (2, 1)


def test_parse_counts_splits_staged_from_unstaged() -> None:
    counts = git_status.parse_counts(PORCELAIN)
    # Four tracked entries changed; the one with `MM` is in both columns at once.
    assert counts.changed == 4
    assert (counts.staged, counts.unstaged) == (3, 2)
    assert (counts.untracked, counts.conflicted) == (2, 1)


def test_parse_counts_of_a_clean_detached_checkout() -> None:
    counts = git_status.parse_counts("# branch.oid 1c2d3e4\n# branch.head (detached)\n")
    assert counts == git_status.Counts()


def test_parse_shortstat() -> None:
    assert git_status.parse_shortstat(" 3 files changed, 42 insertions(+), 7 deletions(-)\n") == (42, 7)
    assert git_status.parse_shortstat(" 1 file changed, 1 insertion(+)\n") == (1, 0)
    assert git_status.parse_shortstat(" 1 file changed, 1 deletion(-)\n") == (0, 1)
    assert git_status.parse_shortstat("") == (0, 0)


# ── reading a real repo ────────────────────────────────────────────────────────


def test_a_fresh_checkout_is_clean(workbench_home: Path) -> None:
    workspace = workspace_at(workbench_home)
    make_repo(workspace.path)

    status = run(git_status.read_status(workspace))
    assert status.state == git_status.CLEAN
    assert status.branch == "main"
    assert status.subject == "first commit"
    assert status.head and status.committed  # a short sha and a relative date, whatever they are
    assert (status.changed, status.untracked, status.insertions, status.deletions) == (0, 0, 0, 0)


def test_edits_make_it_dirty_and_are_counted(workbench_home: Path) -> None:
    workspace = workspace_at(workbench_home)
    make_repo(workspace.path, content="one\ntwo\nthree\n")
    (workspace.path / "file.txt").write_text("one\ntwo\nthree\nfour\n")
    (workspace.path / "staged.txt").write_text("new\n")
    git(workspace.path, "add", "staged.txt")
    (workspace.path / "untracked.txt").write_text("loose\n")

    status = run(git_status.read_status(workspace))
    assert status.state == git_status.DIRTY
    assert (status.changed, status.staged, status.unstaged, status.untracked) == (2, 1, 1, 1)
    # The added line and the whole staged file, because both are changes against HEAD.
    assert (status.insertions, status.deletions) == (2, 0)


def test_a_conflicted_merge_says_so(workbench_home: Path) -> None:
    workspace = workspace_at(workbench_home)
    make_repo(workspace.path, content="base\n")
    git(workspace.path, "checkout", "-b", "other")
    (workspace.path / "file.txt").write_text("theirs\n")
    git(workspace.path, "commit", "-am", "theirs")
    git(workspace.path, "checkout", "main")
    (workspace.path / "file.txt").write_text("ours\n")
    git(workspace.path, "commit", "-am", "ours")
    subprocess.run(["git", "merge", "other"], cwd=workspace.path, env=_GIT_ENV, capture_output=True)

    status = run(git_status.read_status(workspace))
    assert status.state == git_status.CONFLICTED
    assert status.conflicted == 1


def test_ahead_and_behind_its_upstream(workbench_home: Path) -> None:
    upstream = workbench_home / "upstream.git"
    origin = workbench_home / "origin"
    make_repo(origin)
    git(origin, "clone", "--bare", str(origin), str(upstream))

    workspace = workspace_at(workbench_home)
    git(workbench_home, "clone", str(upstream), str(workspace.path))

    # One commit only we have...
    (workspace.path / "mine.txt").write_text("mine\n")
    git(workspace.path, "add", "mine.txt")
    git(workspace.path, "commit", "-m", "mine")
    # ...and two only upstream has, fetched but not merged.
    (origin / "file.txt").write_text("moved on\n")
    git(origin, "commit", "-am", "theirs")
    (origin / "file.txt").write_text("moved on again\n")
    git(origin, "commit", "-am", "theirs again")
    git(origin, "push", str(upstream), "main")
    git(workspace.path, "fetch", "origin")

    status = run(git_status.read_status(workspace))
    assert status.state == git_status.CLEAN
    assert status.upstream == "origin/main"
    assert (status.ahead, status.behind) == (1, 2)


def test_a_detached_checkout_has_no_branch(workbench_home: Path) -> None:
    workspace = workspace_at(workbench_home)
    make_repo(workspace.path)
    git(workspace.path, "checkout", "--detach", "HEAD")

    status = run(git_status.read_status(workspace))
    assert status.state == git_status.CLEAN
    assert status.branch == ""
    assert status.head  # the card falls back to the sha it's sitting on


# ── workspaces that aren't (yet) a checkout ────────────────────────────────────


def test_a_directory_without_a_repo_is_still_being_created(workbench_home: Path) -> None:
    """The workspace directory is made before the clone that fills it, and the clone takes a while."""
    workspace = workspace_at(workbench_home)
    workspace.path.mkdir()

    status = run(git_status.read_status(workspace))
    assert status.state == git_status.CLONING
    assert status.detail == "not a git repository yet"


def test_a_repo_with_no_commits_is_still_being_created(workbench_home: Path) -> None:
    """`git clone` makes .git first and checks out last, so this is a clone in flight."""
    workspace = workspace_at(workbench_home)
    workspace.path.mkdir()
    git(workspace.path, "init", "-b", "main")

    status = run(git_status.read_status(workspace))
    assert status.state == git_status.CLONING
    assert status.detail == "no commits yet"
    assert status.branch == "main"


def test_a_broken_repo_reports_what_git_said(workbench_home: Path) -> None:
    workspace = workspace_at(workbench_home)
    workspace.path.mkdir()
    (workspace.path / ".git").write_text("not a gitfile\n")

    status = run(git_status.read_status(workspace))
    assert status.state == git_status.UNAVAILABLE
    assert "fatal" in status.detail
    # One line of it: the sidebar has a card to fill, not a terminal.
    assert "\n" not in status.detail


# ── many at once ───────────────────────────────────────────────────────────────


def test_read_statuses_answers_for_every_workspace(workbench_home: Path) -> None:
    clean = workspace_at(workbench_home, name="clean")
    make_repo(clean.path)
    pending = workspace_at(workbench_home, name="pending")
    pending.path.mkdir()

    statuses = run(git_status.read_statuses((clean, pending)))
    assert [s.workspace_id for s in statuses] == ["proj/clean", "proj/pending"]
    assert [s.state for s in statuses] == [git_status.CLEAN, git_status.CLONING]


def test_a_second_read_comes_from_the_cache(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two browser tabs polling in step must not double the `git` processes on a one-core box."""
    workspace = workspace_at(workbench_home)
    make_repo(workspace.path)
    run(git_status.read_statuses((workspace,)))

    reads = 0

    async def counted(ws: Workspace) -> git_status.WorkspaceStatus:
        nonlocal reads
        reads += 1
        return git_status.WorkspaceStatus(workspace_id=ws.id, state=git_status.CLEAN)

    monkeypatch.setattr(git_status, "read_status", counted)
    assert run(git_status.read_statuses((workspace,)))[0].subject == "first commit"
    assert reads == 0


def test_the_cache_forgets_deleted_workspaces(workbench_home: Path) -> None:
    workspace = workspace_at(workbench_home)
    make_repo(workspace.path)
    run(git_status.read_statuses((workspace,)))
    assert workspace.id in git_status._cache

    run(git_status.read_statuses(()))
    assert git_status._cache == {}


# ── the route ──────────────────────────────────────────────────────────────────


def test_status_route_covers_every_workspace_of_every_project(workbench_home: Path) -> None:
    store.add_project("one", "https://example.com/one.git")
    store.add_project("two", "https://example.com/two.git")
    make_repo(workspace_at(workbench_home, project="one", name="a").path)
    workspace_at(workbench_home, project="two", name="b").path.mkdir()

    response = _client().get("/api/workspaces/status")

    assert response.status_code == 200
    body = {entry["workspace_id"]: entry for entry in response.json()}
    assert set(body) == {"one/a", "two/b"}
    assert body["one/a"]["state"] == git_status.CLEAN
    assert body["one/a"]["branch"] == "main"
    assert body["two/b"]["state"] == git_status.CLONING


def test_status_route_with_no_projects_returns_nothing(workbench_home: Path) -> None:
    assert _client().get("/api/workspaces/status").json() == []
