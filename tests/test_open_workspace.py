from __future__ import annotations

import urllib.parse
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
from server.routes import open_workspace as route
from server.tabs import ServerTab
from server.tabs import _tabs


@pytest.fixture(autouse=True)
def clear_tabs() -> Generator[None]:
    _tabs.clear()
    yield
    _tabs.clear()


def _stub_access(monkeypatch: pytest.MonkeyPatch, decision: str, token: str = "") -> None:
    async def fake(repo: str, ref: str) -> git_remote.RepoAccess:
        return git_remote.RepoAccess(decision=decision, token=token)

    monkeypatch.setattr(route, "resolve_access", fake)


def _stub_tabs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
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


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


def _post(
    form: dict[str, str] | None = None,
    json: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> Any:
    kwargs: dict[str, Any] = {}
    if form is not None:
        kwargs["data"] = form
    if json is not None:
        kwargs["json"] = json
    if query is not None:
        kwargs["params"] = query
    return _client().post("/open-workspace", follow_redirects=False, **kwargs)


# ── the contract's error cases ─────────────────────────────────────────────────


def test_missing_repo_is_400(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    assert _post(form={"ref": "main"}).status_code == 400


def test_missing_ref_is_400(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    assert _post(form={"repo": "https://github.com/o/r.git"}).status_code == 400


@pytest.mark.parametrize("repo", ["file:///etc/passwd", "ext::sh -c id", "not-a-url", "/tmp/x"])
def test_bad_transport_is_400(workbench_home: Path, monkeypatch: pytest.MonkeyPatch, repo: str) -> None:
    _stub_access(monkeypatch, "ok")
    assert _post(form={"repo": repo, "ref": "main"}).status_code == 400


@pytest.mark.parametrize("ref", ["-rf", "--upload-pack=x", "a b", "a;b", "a$b", "../etc"])
def test_unsafe_ref_is_400(workbench_home: Path, monkeypatch: pytest.MonkeyPatch, ref: str) -> None:
    _stub_access(monkeypatch, "ok")
    assert _post(form={"repo": "https://github.com/o/r.git", "ref": ref}).status_code == 400


def test_forbidden_is_403(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "forbidden")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 403
    assert "Location" not in resp.headers


def test_not_found_is_404(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "not_found")
    assert _post(form={"repo": "https://github.com/o/r.git", "ref": "main"}).status_code == 404


def test_internal_error_is_500(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "error")
    assert _post(form={"repo": "https://github.com/o/r.git", "ref": "main"}).status_code == 500


def test_a_rejected_request_leaves_no_project_behind(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "forbidden")
    _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert store.load_projects() == ()


# ── the success path ───────────────────────────────────────────────────────────


def test_success_registers_a_project_and_redirects(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    created = _stub_tabs(monkeypatch)

    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 303
    location = resp.headers["Location"]
    assert location.startswith("/?workspace=")
    workspace_id = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["workspace"][0]
    assert workspace_id == "r/main"
    assert (workbench_home / "workspaces" / "r" / "main").is_dir()

    project = store.find_project_by_repo("https://github.com/o/r.git")
    assert project is not None and project.id == "r"

    env = created[0]["env"]
    assert env["WS_REPO"] == "https://github.com/o/r.git"
    assert env["WS_REF"] == "main"
    assert "WS_GITHUB_TOKEN" not in env


def test_workspace_tab_restores_as_a_claude_session_not_a_re_clone(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The script clones and then hands over to Claude. Restoring must re-enter the conversation."""
    _stub_access(monkeypatch, "ok")
    created = _stub_tabs(monkeypatch)
    _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert created[0]["kind"] == "claude"
    assert created[0]["session_id"]


def test_success_passes_the_token_to_the_tab(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok", token="ghs_secret")
    created = _stub_tabs(monkeypatch)
    resp = _post(form={"repo": "https://github.com/o/private.git", "ref": "abc1234"})
    assert resp.status_code == 303
    assert created[0]["env"]["WS_GITHUB_TOKEN"] == "ghs_secret"


def test_a_second_visit_gets_its_own_workspace(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reuse would risk trampling work in progress; a workspace clones from the local mirror, so
    making another one is cheap."""
    _stub_access(monkeypatch, "ok")
    _stub_tabs(monkeypatch)
    first = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    second = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})

    assert first.headers["Location"] != second.headers["Location"]
    assert second.headers["Location"].startswith("/?workspace=r/main-2")
    # ...and still only one project for the repo.
    assert len(store.load_projects()) == 1


def test_an_existing_project_is_reused(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    _stub_tabs(monkeypatch)
    store.add_project("my name for it", "https://github.com/o/r.git")

    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.headers["Location"].startswith("/?workspace=my-name-for-it/main")
    assert len(store.load_projects()) == 1


# ── input shapes the router forces on us ───────────────────────────────────────


def test_accepts_json_body(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    _stub_tabs(monkeypatch)
    assert _post(json={"repo": "https://github.com/o/r.git", "ref": "main"}).status_code == 303


def test_accepts_query_params(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    _stub_tabs(monkeypatch)
    assert _post(query={"repo": "https://github.com/o/r.git", "ref": "main"}).status_code == 303


def test_accepts_get_with_query(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # GET is supported as a workaround for the openhost router's 302→/login bounce, which demotes
    # the eventual POST back to a GET. The same query params must work either way.
    _stub_access(monkeypatch, "ok")
    _stub_tabs(monkeypatch)
    resp = _client().get(
        "/open-workspace", params={"repo": "https://github.com/o/r.git", "ref": "main"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["Location"].startswith("/?workspace=")


def test_debug_route_is_gone() -> None:
    assert _client().get("/debug", params={"repo": "https://github.com/o/r.git"}).status_code == 404
