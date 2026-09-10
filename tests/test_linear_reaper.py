from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.linear import reaper
from server.linear import runs as linear_runs
from server.linear.reaper import PullRequestState
from server.linear.reaper import reap_merged_runs
from server.linear.runs import LinearRun
from server.linear.runs import add_run
from server.linear.runs import load_runs
from server.projects import workspaces
from server.projects.workspaces import Workspace


def _run(workspace_id: str = "md-notes/ENG-7") -> LinearRun:
    return LinearRun(
        workspace_id=workspace_id,
        issue_id="issue-1",
        issue_identifier="ENG-7",
        issue_url="https://linear.app/x/issue/ENG-7",
        repo="cloud-in-a-bottle/md-notes",
        branch="eng-7-fix",
        created_at="2026-09-09T00:00:00+00:00",
    )


def _pr(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    async def fake(run: LinearRun) -> PullRequestState:
        return PullRequestState(state=state, url="https://github.com/o/r/pull/1")

    monkeypatch.setattr(reaper, "pull_request_for", fake)


class TestRunStore:
    def test_a_run_survives_a_round_trip(self, workbench_home: Path) -> None:
        add_run(_run())
        assert load_runs() == (_run(),)

    def test_re_adding_a_workspace_replaces_its_run(self, workbench_home: Path) -> None:
        add_run(_run())
        add_run(_run())
        assert len(load_runs()) == 1

    def test_an_unreadable_file_costs_the_reaper_but_not_the_workbench(self, workbench_home: Path) -> None:
        linear_runs.RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
        linear_runs.RUNS_PATH.write_text("{ not json")
        assert load_runs() == ()


class TestReaper:
    def test_a_merged_pr_deletes_the_workspace(self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = Workspace(project_id="md-notes", name="ENG-7")
        workspaces.create_workspace_dir(workspace)
        add_run(_run())
        _pr(monkeypatch, "MERGED")

        asyncio.run(reap_merged_runs())

        assert not workspace.path.exists()
        assert load_runs() == ()

    def test_an_open_pr_is_left_alone(self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = Workspace(project_id="md-notes", name="ENG-7")
        workspaces.create_workspace_dir(workspace)
        add_run(_run())
        _pr(monkeypatch, "OPEN")

        asyncio.run(reap_merged_runs())

        assert workspace.path.is_dir()
        assert len(load_runs()) == 1

    def test_a_closed_pr_keeps_the_workspace(self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Closed without merging usually means the work was rejected, and the workspace is where
        you would go to find out why."""
        workspace = Workspace(project_id="md-notes", name="ENG-7")
        workspaces.create_workspace_dir(workspace)
        add_run(_run())
        _pr(monkeypatch, "CLOSED")

        asyncio.run(reap_merged_runs())

        assert workspace.path.is_dir()
        assert len(load_runs()) == 1

    def test_a_branch_with_no_pr_yet_is_left_alone(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = Workspace(project_id="md-notes", name="ENG-7")
        workspaces.create_workspace_dir(workspace)
        add_run(_run())
        _pr(monkeypatch, "")

        asyncio.run(reap_merged_runs())

        assert workspace.path.is_dir()

    def test_a_workspace_deleted_by_hand_is_forgotten(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_run(_run())
        _pr(monkeypatch, "OPEN")

        asyncio.run(reap_merged_runs())

        assert load_runs() == ()

    def test_a_run_whose_pr_cannot_be_checked_is_kept(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GitHub outage must not be read as "not merged, and never will be"."""
        workspace = Workspace(project_id="md-notes", name="ENG-7")
        workspaces.create_workspace_dir(workspace)
        add_run(_run())

        async def boom(run: LinearRun) -> PullRequestState:
            raise RuntimeError("gh exploded")

        monkeypatch.setattr(reaper, "pull_request_for", boom)
        asyncio.run(reap_merged_runs())

        assert workspace.path.is_dir()
        assert len(load_runs()) == 1
