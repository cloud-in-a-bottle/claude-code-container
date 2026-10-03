from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from server import app as srv
from server import billing
from server import tabs as tabs_module
from server.projects import archive as archive_ops
from server.projects import archive_store
from server.projects import store
from server.projects import teardown
from server.projects import workspaces
from server.projects.archive_store import ArchivedWorkspace
from server.projects.workspaces import Workspace
from server.tab_store import CLAUDE
from server.tab_store import SHELL
from server.tab_store import PersistedTab
from server.tabs import ServerTab
from server.tabs import _tabs

_SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def clear_tabs() -> Generator[None]:
    _tabs.clear()
    yield
    _tabs.clear()


@pytest.fixture(autouse=True)
def offline_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restoring a tab builds its auth environment; not from the secrets app here."""

    async def fake() -> str:
        return "sk-test"

    monkeypatch.setattr(billing, "get_anthropic_key", fake)


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


def _workspace(name: str = "ws", project_id: str = "r") -> Workspace:
    workspace = Workspace(project_id=project_id, name=name)
    workspaces.create_workspace_dir(workspace)
    return workspace


def _add_tab(workspace: Workspace, tab_id: str, *, kind: str = CLAUDE, session_id: str = _SESSION) -> ServerTab:
    tab = ServerTab(
        id=tab_id,
        label="claude",
        master_fd=-1,
        proc=MagicMock(),
        kind=kind,
        start_cwd=str(workspace.path),
        session_id=session_id,
        workspace_id=workspace.id,
    )
    _tabs[tab_id] = tab
    return tab


def _spy_on_killing(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """These tabs have no real process behind them, so killing is recorded rather than done."""
    killed: list[str] = []
    monkeypatch.setattr(teardown, "kill_tab", lambda t: killed.append(t.id))
    return killed


def _stub_tab_creation(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []

    async def fake_create(**kwargs: Any) -> ServerTab:
        tab = ServerTab(
            id=kwargs.get("tab_id") or f"tab-{len(created)}",
            label=kwargs.get("label") or "test",
            master_fd=-1,
            proc=MagicMock(),
            workspace_id=kwargs.get("workspace_id", ""),
        )
        _tabs[tab.id] = tab
        created.append(kwargs)
        return tab

    monkeypatch.setattr(tabs_module, "create_server_tab", fake_create)
    return created


class TestArchiveStore:
    def test_an_absent_file_is_an_empty_archive(self) -> None:
        assert archive_store.load_archive() == {}

    def test_a_record_survives_a_round_trip(self) -> None:
        tab = PersistedTab(id="t1", label="claude", kind=CLAUDE, cwd="/w", session_id=_SESSION, workspace_id="r/ws")
        archive_store.put_archived("r/ws", (tab,))

        loaded = archive_store.load_archive()
        assert set(loaded) == {"r/ws"}
        assert loaded["r/ws"].tabs == (tab,)
        assert loaded["r/ws"].archived_at > 0

    def test_dropping_leaves_the_other_entries(self) -> None:
        archive_store.put_archived("r/a", ())
        archive_store.put_archived("r/b", ())
        archive_store.drop_archived("r/a")
        assert set(archive_store.load_archive()) == {"r/b"}

    def test_dropping_something_unarchived_is_not_an_error(self) -> None:
        archive_store.drop_archived("r/nothing")
        assert archive_store.load_archive() == {}

    def test_a_malformed_file_raises_rather_than_reporting_nothing_archived(self) -> None:
        """Reporting an empty archive would present every archived workspace as a live one, which
        is how Claude gets restarted in a workspace that was deliberately put away."""
        archive_store.ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        archive_store.ARCHIVE_PATH.write_text('["not an object"]')
        with pytest.raises(ValueError, match="malformed archive"):
            archive_store.load_archive()

    def test_tabs_are_written_in_the_same_shape_as_the_tab_list(self) -> None:
        """The two files share a codec, so a tab archived by one build restores in the next."""
        tab = PersistedTab(id="t1", label="term 2", kind=SHELL, cwd="/w", workspace_id="r/ws")
        archive_store.put_archived("r/ws", (tab,))
        raw = json.loads(archive_store.ARCHIVE_PATH.read_text())
        assert raw["r/ws"]["tabs"] == [
            {"id": "t1", "label": "term 2", "kind": SHELL, "cwd": "/w", "session_id": "", "workspace_id": "r/ws"}
        ]


class TestArchiving:
    def test_it_closes_the_tabs_and_writes_down_what_they_were(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace()
        _add_tab(workspace, "t1")
        elsewhere = _add_tab(_workspace("other"), "t2")
        killed = _spy_on_killing(monkeypatch)

        record = asyncio.run(archive_ops.archive_workspace(workspace))

        assert killed == ["t1"]
        assert list(_tabs) == [elsewhere.id]
        assert [t.id for t in record.tabs] == ["t1"]
        assert record.tabs[0].session_id == _SESSION
        assert record.tabs[0].kind == CLAUDE

    def test_the_directory_is_left_alone(self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point: archiving is about processes, not about the work on disk."""
        workspace = _workspace()
        (workspace.path / "uncommitted.txt").write_text("in progress\n")
        _add_tab(workspace, "t1")
        _spy_on_killing(monkeypatch)

        asyncio.run(archive_ops.archive_workspace(workspace))

        assert (workspace.path / "uncommitted.txt").read_text() == "in progress\n"

    def test_it_keeps_the_workspace_billing_mode(self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unlike deleting, which forgets it — the tabs are coming back and must be paid for the
        same way they were before."""
        workspace = _workspace()
        billing.pin_workspace_mode(workspace.id, billing.SUBSCRIPTION)
        _spy_on_killing(monkeypatch)

        asyncio.run(archive_ops.archive_workspace(workspace))

        assert billing.workspace_mode(workspace.id) == billing.SUBSCRIPTION

    def test_a_workspace_with_no_terminals_archives_anyway(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace()
        _spy_on_killing(monkeypatch)

        asyncio.run(archive_ops.archive_workspace(workspace))

        assert archive_store.is_archived(workspace.id)


class TestUnarchiving:
    def test_it_reopens_each_tab_in_the_session_it_left(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace()
        archive_store.put_archived(
            workspace.id,
            (
                PersistedTab(
                    id="t1",
                    label="claude",
                    kind=CLAUDE,
                    cwd=str(workspace.path),
                    session_id=_SESSION,
                    workspace_id=workspace.id,
                ),
                PersistedTab(id="t2", label="term 2", kind=SHELL, cwd=str(workspace.path), workspace_id=workspace.id),
            ),
        )
        created = _stub_tab_creation(monkeypatch)

        restored = asyncio.run(archive_ops.unarchive_workspace(workspace))

        assert [t.id for t in restored] == ["t1", "t2"]
        assert not archive_store.is_archived(workspace.id)
        # The Claude tab resumes rather than starting a new conversation, and the shell is a shell.
        assert f"--resume {_SESSION}" in " ".join(created[0]["command"])
        assert created[1]["command"] == ["bash", "-l"]

    def test_the_restored_tabs_keep_their_ids_and_labels(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A browser holding a ?tab= link, or a remembered dock layout, still resolves afterwards."""
        workspace = _workspace()
        archive_store.put_archived(
            workspace.id,
            (
                PersistedTab(
                    id="t1", label="dev server", kind=SHELL, cwd=str(workspace.path), workspace_id=workspace.id
                ),
            ),
        )
        created = _stub_tab_creation(monkeypatch)

        asyncio.run(archive_ops.unarchive_workspace(workspace))

        assert created[0]["tab_id"] == "t1"
        assert created[0]["label"] == "dev server"

    def test_a_tab_whose_directory_is_gone_lands_in_the_workspace_root(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace()
        archive_store.put_archived(
            workspace.id,
            (
                PersistedTab(
                    id="t1",
                    label="claude",
                    kind=CLAUDE,
                    cwd=str(workspace.path / "deleted-subdir"),
                    session_id=_SESSION,
                    workspace_id=workspace.id,
                ),
            ),
        )
        created = _stub_tab_creation(monkeypatch)

        asyncio.run(archive_ops.unarchive_workspace(workspace))

        assert created[0]["cwd"] == str(workspace.path)

    def test_unarchiving_something_that_is_not_archived_does_nothing(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace()
        created = _stub_tab_creation(monkeypatch)

        assert asyncio.run(archive_ops.unarchive_workspace(workspace)) == []
        assert created == []

    def test_a_round_trip_brings_the_same_terminals_back(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = _workspace()
        _add_tab(workspace, "t1")
        _add_tab(workspace, "t2", kind=SHELL, session_id="")
        _spy_on_killing(monkeypatch)
        asyncio.run(archive_ops.archive_workspace(workspace))
        assert _tabs == {}

        created = _stub_tab_creation(monkeypatch)
        restored = asyncio.run(archive_ops.unarchive_workspace(workspace))

        assert [t.id for t in restored] == ["t1", "t2"]
        assert [c["kind"] for c in created] == [CLAUDE, SHELL]
        assert [c["session_id"] for c in created] == [_SESSION, ""]


class TestStartupRestore:
    def test_an_archived_workspace_has_nothing_left_in_the_tab_list_to_restore(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Archiving kills the tabs, which takes them out of tabs.json — which is what lets the
        startup restore stay ignorant of the archive entirely."""
        archived = _workspace("archived")
        live = _workspace("live")
        _add_tab(archived, "t1")
        _add_tab(live, "t2")
        tabs_module.persist_tabs()
        _spy_on_killing(monkeypatch)

        asyncio.run(archive_ops.archive_workspace(archived))

        _tabs.clear()
        created = _stub_tab_creation(monkeypatch)
        restored = asyncio.run(tabs_module.restore_tabs())

        assert [t.id for t in restored] == ["t2"]
        assert [c["workspace_id"] for c in created] == [live.id]


class TestArchiveRoutes:
    def test_archiving_reports_the_workspace_as_archived(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.add_project("r", "https://github.com/o/r.git")
        workspace = _workspace()
        _add_tab(workspace, "t1")
        killed = _spy_on_killing(monkeypatch)

        resp = _client().post("/api/workspaces/r/ws/archive")

        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        assert killed == ["t1"]

    def test_the_sidebar_sees_which_workspaces_are_archived(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.add_project("r", "https://github.com/o/r.git")
        _workspace("kept")
        _workspace("put-away")
        _spy_on_killing(monkeypatch)
        client = _client()
        assert client.post("/api/workspaces/r/put-away/archive").status_code == 200

        listed = {w["name"]: w["archived"] for w in client.get("/api/projects").json()[0]["workspaces"]}
        assert listed == {"kept": False, "put-away": True}

    def test_unarchiving_returns_the_terminals_it_reopened(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.add_project("r", "https://github.com/o/r.git")
        workspace = _workspace()
        archive_store.put_archived(
            workspace.id,
            (
                PersistedTab(
                    id="t1",
                    label="claude",
                    kind=CLAUDE,
                    cwd=str(workspace.path),
                    session_id=_SESSION,
                    workspace_id=workspace.id,
                ),
            ),
        )
        _stub_tab_creation(monkeypatch)

        body = _client().post("/api/workspaces/r/ws/unarchive").json()

        assert body["archived"] is False
        assert [t["id"] for t in body["tabs"]] == ["t1"]

    def test_a_terminal_cannot_be_opened_in_an_archived_workspace(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It would leave the workspace archived in the rail and running underneath."""
        store.add_project("r", "https://github.com/o/r.git")
        workspace = _workspace()
        _spy_on_killing(monkeypatch)
        client = _client()
        assert client.post("/api/workspaces/r/ws/archive").status_code == 200

        resp = client.post("/api/tabs", json={"workspace_id": workspace.id})

        assert resp.status_code == 409
        assert resp.json()["error"] == "archived"

    def test_an_editor_cannot_be_started_in_an_archived_workspace(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.add_project("r", "https://github.com/o/r.git")
        workspace = _workspace()
        _spy_on_killing(monkeypatch)
        client = _client()
        assert client.post("/api/workspaces/r/ws/archive").status_code == 200

        resp = client.post("/api/editor", json={"workspace_id": workspace.id})

        assert resp.status_code == 409

    def test_archived_workspaces_are_left_out_of_the_status_poll(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sparing them the `git` is half of what archiving is for."""
        store.add_project("r", "https://github.com/o/r.git")
        _workspace("kept")
        _workspace("put-away")
        _spy_on_killing(monkeypatch)
        client = _client()
        assert client.post("/api/workspaces/r/put-away/archive").status_code == 200

        reported = [s["workspace_id"] for s in client.get("/api/workspaces/status").json()]
        assert reported == ["r/kept"]

    def test_archived_workspaces_are_left_out_of_the_agent_poll(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.add_project("r", "https://github.com/o/r.git")
        _workspace("kept")
        _workspace("put-away")
        _spy_on_killing(monkeypatch)
        client = _client()
        assert client.post("/api/workspaces/r/put-away/archive").status_code == 200

        reported = [a["workspace_id"] for a in client.get("/api/workspaces/agents").json()]
        assert "r/put-away" not in reported

    def test_archiving_will_not_walk_out_of_its_project(self, workbench_home: Path) -> None:
        store.add_project("r", "https://github.com/o/r.git")
        _workspace("keep")
        assert _client().post("/api/workspaces/r/%2E%2E/archive").status_code in (400, 404)
        assert not archive_store.load_archive()

    def test_deleting_an_archived_workspace_forgets_its_archive_entry(
        self, workbench_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the file keeps a row, and its saved tabs, for a directory that is gone."""
        store.add_project("r", "https://github.com/o/r.git")
        workspace = _workspace()
        _spy_on_killing(monkeypatch)
        client = _client()
        assert client.post("/api/workspaces/r/ws/archive").status_code == 200

        assert client.delete("/api/workspaces/r/ws").status_code == 200
        assert not workspace.path.exists()
        assert archive_store.load_archive() == {}


def test_an_archived_record_reports_what_it_holds() -> None:
    record = ArchivedWorkspace(workspace_id="r/ws", archived_at=1.0)
    assert record.tabs == ()
