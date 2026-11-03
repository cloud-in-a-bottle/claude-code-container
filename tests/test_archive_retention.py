from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from server import app as srv
from server import billing
from server.projects import archive_store
from server.projects import retention
from server.projects import workspaces
from server.projects.archive_reaper import reap_expired_archives
from server.projects.archive_store import ArchivedWorkspace
from server.projects.retention import DEFAULT_RETENTION_DAYS
from server.projects.retention import load_retention_days
from server.projects.retention import save_retention_days
from server.projects.workspaces import Workspace
from server.routes import settings as settings_routes

_DAY = 24 * 60 * 60


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings page also probes both credentials; neither should leave the test."""

    async def no_key() -> str:
        return ""

    async def no_subscription() -> dict[str, Any]:
        return {"logged_in": False, "method": "none", "account": "", "detail": ""}

    monkeypatch.setattr(billing, "get_anthropic_key", no_key)
    monkeypatch.setattr(settings_routes, "subscription_status", no_subscription)


def _archived(name: str, days_ago: float) -> Workspace:
    workspace = Workspace(project_id="r", name=name)
    workspaces.create_workspace_dir(workspace)
    record = ArchivedWorkspace(workspace_id=workspace.id, archived_at=time.time() - days_ago * _DAY)
    archive_store.save_archive({**archive_store.load_archive(), workspace.id: record})
    return workspace


class TestRetentionSetting:
    def test_defaults_to_fourteen_days(self) -> None:
        assert load_retention_days() == DEFAULT_RETENTION_DAYS == 14

    def test_a_choice_survives_a_round_trip(self) -> None:
        save_retention_days(30)
        assert load_retention_days() == 30

    def test_a_malformed_file_raises_rather_than_falling_back_to_the_default(self) -> None:
        """Falling back would delete work on a schedule nobody chose."""
        retention.RETENTION_PATH.parent.mkdir(parents=True, exist_ok=True)
        retention.RETENTION_PATH.write_text('{"days": "soon"}')
        with pytest.raises(ValueError, match="malformed archive retention"):
            load_retention_days()

    def test_negative_days_are_refused(self) -> None:
        with pytest.raises(ValueError):
            save_retention_days(-1)


class TestReaper:
    def test_an_expired_archive_is_deleted_and_forgotten(self) -> None:
        old = _archived("old", days_ago=15)
        asyncio.run(reap_expired_archives())
        assert not old.path.exists()
        assert old.id not in archive_store.load_archive()

    def test_a_recent_archive_is_kept(self) -> None:
        recent = _archived("recent", days_ago=13)
        asyncio.run(reap_expired_archives())
        assert recent.path.is_dir()
        assert recent.id in archive_store.load_archive()

    def test_the_setting_moves_the_cutoff(self) -> None:
        save_retention_days(3)
        workspace = _archived("ws", days_ago=4)
        asyncio.run(reap_expired_archives())
        assert not workspace.path.exists()

    def test_zero_keeps_archives_forever(self) -> None:
        save_retention_days(0)
        ancient = _archived("ancient", days_ago=1000)
        asyncio.run(reap_expired_archives())
        assert ancient.path.is_dir()

    def test_a_live_workspace_is_never_deleted_however_old(self) -> None:
        live = Workspace(project_id="r", name="live")
        workspaces.create_workspace_dir(live)
        asyncio.run(reap_expired_archives(now=time.time() + 1000 * _DAY))
        assert live.path.is_dir()

    def test_an_entry_without_an_archive_time_is_skipped(self) -> None:
        """A missing timestamp reads as 0, which must not mean "archived longest ago"."""
        workspace = _archived("undated", days_ago=0)
        archive_store.save_archive({workspace.id: ArchivedWorkspace(workspace_id=workspace.id, archived_at=0.0)})
        asyncio.run(reap_expired_archives())
        assert workspace.path.is_dir()

    def test_an_entry_whose_directory_is_gone_is_forgotten(self) -> None:
        workspace = _archived("gone", days_ago=1)
        workspaces.delete_workspace(workspace)
        asyncio.run(reap_expired_archives())
        assert archive_store.load_archive() == {}


class TestSettingsRoute:
    def test_the_settings_page_reports_the_retention(self) -> None:
        assert _client().get("/api/settings").json()["archive_retention_days"] == 14

    def test_retention_can_be_changed_without_touching_billing(self) -> None:
        resp = _client().post("/api/settings", json={"archive_retention_days": 7})
        assert resp.status_code == 200
        assert resp.json()["archive_retention_days"] == 7
        assert load_retention_days() == 7
        assert billing.load_billing().default_mode == billing.DEFAULT_MODE

    @pytest.mark.parametrize("bad", [-1, 1.5, "7", True, None])
    def test_a_bad_value_is_rejected_and_changes_nothing(self, bad: object) -> None:
        resp = _client().post("/api/settings", json={"archive_retention_days": bad, "default_billing": "subscription"})
        assert resp.status_code == 400
        assert load_retention_days() == 14
        assert billing.load_billing().default_mode == billing.DEFAULT_MODE
