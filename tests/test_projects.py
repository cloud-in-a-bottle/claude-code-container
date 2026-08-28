from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from server import app as srv
from server import git_remote
from server.projects import launch
from server.projects import store
from server.projects import workspaces
from server.projects.workspaces import Workspace
from server.routes import projects as project_routes
from server.tabs import ServerTab
from server.tabs import _tabs


@pytest.fixture(autouse=True)
def clear_tabs() -> Generator[None]:
    _tabs.clear()
    yield
    _tabs.clear()


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


def _stub_access(monkeypatch: pytest.MonkeyPatch, decision: str = "ok", token: str = "") -> None:
    async def fake(repo: str, ref: str) -> git_remote.RepoAccess:
        return git_remote.RepoAccess(decision=decision, token=token)

    monkeypatch.setattr(project_routes, "resolve_access", fake)


def _stub_tabs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub tab creation so nothing spawns a PTY; returns a log of the calls."""
    created: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> ServerTab:
        tab = ServerTab(
            id=f"tab-{len(created)}",
            label=kwargs.get("label") or "test",
            master_fd=-1,
            proc=MagicMock(),
            workspace_id=kwargs.get("workspace_id", ""),
        )
        _tabs[tab.id] = tab
        created.append(kwargs)
        return tab

    monkeypatch.setattr(launch, "create_server_tab", fake)
    return created


# ── project store ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name, slug",
    [
        ("openhost", "openhost"),
        ("Claude Code Container", "claude-code-container"),
        ("  spaces  ", "spaces"),
        ("!!!", "project"),
        ("Ünïcode", "n-code"),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert store.slugify(name) == slug


def test_unique_project_id_suffixes_collisions() -> None:
    assert store.unique_project_id("repo", frozenset()) == "repo"
    assert store.unique_project_id("repo", frozenset({"repo"})) == "repo-2"
    assert store.unique_project_id("repo", frozenset({"repo", "repo-2"})) == "repo-3"


def test_projects_round_trip(workbench_home: Path) -> None:
    assert store.load_projects() == ()
    project = store.add_project("md notes", "https://example.com/md.git", setup="just setup")
    assert project.id == "md-notes"
    assert store.load_projects() == (project,)
    assert store.find_project("md-notes") == project
    assert store.find_project_by_repo("https://example.com/md.git") == project

    store.remove_project("md-notes")
    assert store.load_projects() == ()


def test_two_projects_on_the_same_name_get_distinct_ids(workbench_home: Path) -> None:
    first = store.add_project("repo", "https://example.com/a/repo.git")
    second = store.add_project("repo", "https://example.com/b/repo.git")
    assert (first.id, second.id) == ("repo", "repo-2")


def _write_project_file(workbench_home: Path, body: str) -> None:
    path = workbench_home / ".workbench" / "projects.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_malformed_project_file_raises(workbench_home: Path) -> None:
    """Better a loud error than silently forgetting where someone's work lives."""
    _write_project_file(workbench_home, '{"not": "a list"}')
    with pytest.raises(ValueError, match="expected a list"):
        store.load_projects()


def test_project_id_from_disk_is_validated(workbench_home: Path) -> None:
    _write_project_file(workbench_home, '[{"id": "../escape", "repo_url": "https://example.com/r.git"}]')
    with pytest.raises(ValueError, match="malformed project id"):
        store.load_projects()


# ── workspaces on disk ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("workspace_id", ["proj/../../etc", "proj/..", "../proj/ws", "proj/", "proj", "proj/a b"])
def test_parse_workspace_id_rejects_unsafe_ids(workspace_id: str) -> None:
    assert workspaces.parse_workspace_id(workspace_id) is None


def test_parse_workspace_id_accepts_a_plain_pair() -> None:
    parsed = workspaces.parse_workspace_id("my-proj/fix-503")
    assert parsed == Workspace(project_id="my-proj", name="fix-503")


def test_workspaces_are_read_back_off_disk(workbench_home: Path) -> None:
    assert workspaces.list_workspaces("proj") == ()
    for name in ("b-second", "a-first"):
        workspaces.create_workspace_dir(Workspace(project_id="proj", name=name))
    assert [w.name for w in workspaces.list_workspaces("proj")] == ["a-first", "b-second"]


def test_workspace_path_is_under_its_project(workbench_home: Path) -> None:
    workspace = Workspace(project_id="proj", name="fix-503")
    assert workspace.path == workbench_home / "workspaces" / "proj" / "fix-503"
    assert workspace.id == "proj/fix-503"


def test_unique_workspace_name_cleans_and_dedupes(workbench_home: Path) -> None:
    assert workspaces.unique_workspace_name("proj", "feature/nice thing") == "feature-nice-thing"
    workspaces.create_workspace_dir(Workspace(project_id="proj", name="main"))
    assert workspaces.unique_workspace_name("proj", "main") == "main-2"
    workspaces.create_workspace_dir(Workspace(project_id="proj", name="main-2"))
    assert workspaces.unique_workspace_name("proj", "main") == "main-3"


def test_unique_workspace_name_falls_back_when_nothing_survives_cleaning(workbench_home: Path) -> None:
    assert workspaces.unique_workspace_name("proj", "///") == "workspace"


def test_delete_workspace_removes_the_directory(workbench_home: Path) -> None:
    workspace = Workspace(project_id="proj", name="doomed")
    workspaces.create_workspace_dir(workspace)
    (workspace.path / "file.txt").write_text("work")
    workspaces.delete_workspace(workspace)
    assert not workspace.path.exists()


# ── the API ────────────────────────────────────────────────────────────────────


def test_create_and_list_projects(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch)
    client = _client()
    assert client.get("/api/projects").json() == []

    resp = client.post("/api/projects", json={"repo_url": "https://github.com/o/r.git"})
    assert resp.status_code == 200
    assert resp.json() == {
        "id": "r",
        "name": "r",
        "repo_url": "https://github.com/o/r.git",
        "setup": "",
        "workspaces": [],
    }
    assert [p["id"] for p in client.get("/api/projects").json()] == ["r"]


def test_create_project_rejects_a_bad_url(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch)
    resp = _client().post("/api/projects", json={"repo_url": "file:///etc/passwd"})
    assert resp.status_code == 400
    assert store.load_projects() == ()


def test_create_project_reports_an_unreachable_repo(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail here, once, rather than in every workspace the project would go on to create."""
    _stub_access(monkeypatch, "not_found")
    resp = _client().post("/api/projects", json={"repo_url": "https://github.com/o/ghost.git"})
    assert resp.status_code == 404
    assert store.load_projects() == ()


def test_update_project_changes_name_and_setup(workbench_home: Path) -> None:
    store.add_project("r", "https://github.com/o/r.git")
    resp = _client().patch("/api/projects/r", json={"setup": "just setup"})
    assert resp.status_code == 200
    updated = store.find_project("r")
    assert updated is not None and updated.setup == "just setup"


def test_delete_project_refuses_while_it_still_has_workspaces(workbench_home: Path) -> None:
    store.add_project("r", "https://github.com/o/r.git")
    workspaces.create_workspace_dir(Workspace(project_id="r", name="ws"))
    resp = _client().delete("/api/projects/r")
    assert resp.status_code == 409
    assert store.find_project("r") is not None


def test_delete_project_removes_its_mirror(workbench_home: Path) -> None:
    store.add_project("r", "https://github.com/o/r.git")
    mirror = workspaces.mirror_path("r")
    mirror.mkdir(parents=True)
    (mirror / "HEAD").write_text("ref: refs/heads/main\n")

    assert _client().delete("/api/projects/r").status_code == 200
    assert store.load_projects() == ()
    assert not mirror.exists()


def test_create_workspace_makes_the_dir_and_a_claude_tab(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_access(monkeypatch, token="ghs_tok")
    created = _stub_tabs(monkeypatch)
    store.add_project("r", "https://github.com/o/r.git", setup="just setup")

    resp = _client().post("/api/workspaces", json={"project_id": "r", "name": "fix-503"})
    assert resp.status_code == 200
    assert resp.json()["id"] == "r/fix-503"
    assert (workbench_home / "workspaces" / "r" / "fix-503").is_dir()

    assert len(created) == 1
    env = created[0]["env"]
    assert env["WS_REPO"] == "https://github.com/o/r.git"
    assert env["WS_MIRROR"] == str(workspaces.mirror_path("r"))
    assert env["WS_SETUP"] == "just setup"
    assert env["WS_GITHUB_TOKEN"] == "ghs_tok"
    # The bootstrap script ends in a Claude session, so that is what a restore must bring back --
    # never a second clone.
    assert created[0]["kind"] == "claude"
    assert created[0]["workspace_id"] == "r/fix-503"


def test_creating_the_same_workspace_name_twice_makes_two(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_access(monkeypatch)
    _stub_tabs(monkeypatch)
    store.add_project("r", "https://github.com/o/r.git")
    client = _client()

    first = client.post("/api/workspaces", json={"project_id": "r", "name": "main"}).json()
    second = client.post("/api/workspaces", json={"project_id": "r", "name": "main"}).json()
    assert (first["name"], second["name"]) == ("main", "main-2")


def test_create_workspace_rejects_an_unsafe_ref(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch)
    _stub_tabs(monkeypatch)
    store.add_project("r", "https://github.com/o/r.git")
    resp = _client().post("/api/workspaces", json={"project_id": "r", "ref": "--upload-pack=x"})
    assert resp.status_code == 400


def test_delete_workspace_kills_its_tabs(workbench_home: Path) -> None:
    workspace = Workspace(project_id="r", name="ws")
    workspaces.create_workspace_dir(workspace)
    tab = ServerTab(id="t1", label="claude", master_fd=-1, proc=MagicMock(), workspace_id="r/ws")
    other = ServerTab(id="t2", label="claude", master_fd=-1, proc=MagicMock(), workspace_id="r/keep")
    _tabs.update({"t1": tab, "t2": other})

    assert _client().delete("/api/workspaces/r/ws").status_code == 200
    assert not workspace.path.exists()
    assert list(_tabs) == ["t2"]


def test_delete_workspace_will_not_walk_out_of_its_project(workbench_home: Path) -> None:
    """`..` would otherwise resolve to the project's whole workspace directory."""
    keep = Workspace(project_id="r", name="keep")
    workspaces.create_workspace_dir(keep)
    # Written percent-encoded: an unescaped `..` is normalised away by the client before it is
    # ever sent, which would make this test pass without exercising the guard at all.
    resp = _client().delete("/api/workspaces/r/%2E%2E")
    assert resp.status_code == 400
    assert keep.path.is_dir()
