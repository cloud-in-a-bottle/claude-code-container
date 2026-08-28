from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from server import tab_store
from server import tabs as tabs_module
from server.projects import workspaces
from server.projects.workspaces import Workspace
from server.tabs import ServerTab
from server.tabs import _tabs


@pytest.fixture(autouse=True)
def clear_tabs() -> Generator[None]:
    _tabs.clear()
    yield
    _tabs.clear()


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

    async def no_key() -> str:
        return ""

    monkeypatch.setattr(tabs_module, "create_server_tab", fake_create)
    monkeypatch.setattr(tabs_module, "get_anthropic_key", no_key)
    return created


def _write_tabs(workbench_home: Path, monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, str]]) -> None:
    path = workbench_home / ".workbench" / "tabs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))
    monkeypatch.setattr(tab_store, "TABS_PATH", path)


def test_restore_skips_tabs_whose_workspace_is_gone(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal only means anything inside its workspace, so a deleted workspace takes its tabs
    with it rather than dumping them somewhere arbitrary."""
    live = Workspace(project_id="proj", name="live")
    workspaces.create_workspace_dir(live)
    _write_tabs(
        workbench_home,
        monkeypatch,
        [
            {"id": "a", "label": "claude", "kind": "claude", "cwd": str(live.path), "workspace_id": live.id},
            {"id": "b", "label": "claude", "kind": "claude", "cwd": "/gone", "workspace_id": "proj/deleted"},
            # Written before workspaces existed: no workspace to put it back in.
            {"id": "c", "label": "term 2", "kind": "shell", "cwd": "/tmp"},
        ],
    )
    created = _stub_tab_creation(monkeypatch)

    restored = asyncio.run(tabs_module.restore_tabs())
    assert [t.id for t in restored] == ["a"]
    assert created[0]["workspace_id"] == live.id


def test_restore_falls_back_to_the_workspace_root_when_the_cwd_is_gone(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = Workspace(project_id="proj", name="live")
    workspaces.create_workspace_dir(live)
    _write_tabs(
        workbench_home,
        monkeypatch,
        [
            {
                "id": "a",
                "label": "claude",
                "kind": "claude",
                "cwd": str(live.path / "deleted-subdir"),
                "workspace_id": live.id,
            }
        ],
    )
    created = _stub_tab_creation(monkeypatch)

    asyncio.run(tabs_module.restore_tabs())
    assert created[0]["cwd"] == str(live.path)


def test_first_tab_in_a_workspace_runs_claude_and_the_next_does_not(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(project_id="proj", name="ws")
    workspaces.create_workspace_dir(workspace)
    created = _stub_tab_creation(monkeypatch)

    first = asyncio.run(tabs_module.new_tab_in_workspace(workspace))
    assert created[0]["kind"] == tab_store.CLAUDE
    assert created[0]["session_id"]
    assert first.workspace_id == workspace.id

    asyncio.run(tabs_module.new_tab_in_workspace(workspace))
    assert created[1]["kind"] == tab_store.SHELL


def test_a_workspace_only_sees_its_own_tabs() -> None:
    _tabs["a"] = ServerTab(id="a", label="x", master_fd=-1, proc=MagicMock(), workspace_id="p/one")
    _tabs["b"] = ServerTab(id="b", label="y", master_fd=-1, proc=MagicMock(), workspace_id="p/two")
    assert [t.id for t in tabs_module.tabs_for_workspace("p/one")] == ["a"]


@pytest.mark.parametrize("pid", [0, 1, -1, MagicMock().pid])
def test_kill_tab_refuses_to_signal_anything_that_is_not_a_tab(pid: object) -> None:
    """pid 1 in the container is tini, which forwards what it gets to the server.

    A stubbed process is the realistic way to get here: `MagicMock().pid` is not an int but coerces
    to 1 through __index__, so `os.kill(tab.proc.pid, SIGHUP)` used to read as `kill 1` and take the
    whole workbench down — which is how running this suite inside the workbench restarted it.
    """
    tab = ServerTab(id="x", label="x", master_fd=-1, proc=MagicMock(pid=pid))
    with pytest.raises(ValueError, match="refusing to signal"):
        tabs_module.kill_tab(tab)


def test_kill_tab_ends_the_shells_children_too(workbench_home: Path) -> None:
    """A tab's shell has children — Claude, a dev server — and they go with it.

    Signalling only the shell left them running with no terminal attached to them.
    """

    async def spawn_then_kill() -> int:
        tab = await tabs_module.create_server_tab(
            command=["bash", "-c", "sleep 300 & sleep 300"],
            cwd=str(workbench_home),
            label="t",
        )
        pgid = os.getpgid(tab.proc.pid)
        os.killpg(pgid, 0)  # the whole tab is alive, in a group of its own
        # Killed inside the loop on purpose: tab_reader is parked in a blocking os.read() on an
        # executor thread, and only the PTY closing lets asyncio shut that pool down.
        tabs_module.kill_tab(tab)
        return pgid

    pgid = asyncio.run(spawn_then_kill())

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError("the tab's process group outlived kill_tab")
