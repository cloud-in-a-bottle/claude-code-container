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
from server import config
from server import ui_settings
from server.editor import extensions
from server.editor import instances
from server.editor import paths
from server.editor import proxy
from server.editor import settings
from server.editor.instances import EditorInstance
from server.projects import workspaces
from server.projects.workspaces import Workspace


@pytest.fixture(autouse=True)
def never_signal_a_real_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The instances in these tests are mocks, and their `pid` is a mock too. Reaching the real
    os.killpg() with one would send a signal to whatever number it coerced to — including, on app
    shutdown at the end of a TestClient block, a process group that has nothing to do with a test.
    The guard inside _terminate() is tested directly instead, below."""

    async def no_signal(proc: Any) -> None:
        return None

    monkeypatch.setattr(instances, "_terminate", no_signal)


@pytest.fixture(autouse=True)
def clear_instances() -> Generator[None]:
    instances._instances.clear()
    instances._locks.clear()
    yield
    instances._instances.clear()
    instances._locks.clear()


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


def _fake_instance(workspace_id: str, last_used: float, *, alive: bool = True) -> EditorInstance:
    proc = MagicMock()
    proc.returncode = None if alive else 0
    instance = EditorInstance(
        workspace_id=workspace_id,
        socket_path=instances.socket_path(workspace_id),
        user_data_dir=instances.user_data_dir(workspace_id),
        proc=proc,
        last_used=last_used,
    )
    instances._instances[workspace_id] = instance
    return instance


# ---- the path the browser asks for, and the one the editor gets ----------------------------


def test_the_upstream_path_keeps_its_trailing_slash_exactly_as_sent() -> None:
    """Litestar's mount appends a slash to scope['path'], which would 404 every asset. The proxy
    reads the raw path instead, so this is the regression test for that."""
    asset = proxy.parse_target("/vscode/proj/main/stable-abc123/static/out/vs/workbench/main.js")
    assert asset is not None
    assert asset.upstream_path == "/stable-abc123/static/out/vs/workbench/main.js"
    assert asset.workspace == Workspace(project_id="proj", name="main")


def test_the_root_of_an_editor_is_a_bare_slash() -> None:
    target = proxy.parse_target("/vscode/proj/main/")
    assert target is not None
    assert target.upstream_path == "/"


def test_a_workspace_with_no_trailing_slash_still_resolves() -> None:
    target = proxy.parse_target("/vscode/proj/main")
    assert target is not None
    assert target.upstream_path == "/"


@pytest.mark.parametrize(
    "path",
    [
        "/vscode/../../etc/passwd",
        "/vscode/proj/../../../etc",
        "/vscode/proj/.hidden/x",
        "/vscode/proj",
        "/vscode/",
        "/elsewhere/proj/main/",
    ],
)
def test_paths_that_do_not_name_a_legal_workspace_are_refused(path: str) -> None:
    assert proxy.parse_target(path) is None


def test_percent_encoding_is_passed_through_untouched() -> None:
    """Re-encoding here would corrupt paths the editor built itself."""
    target = proxy.parse_target("/vscode/proj/main/file%20with%20spaces.py")
    assert target is not None
    assert target.upstream_path == "/file%20with%20spaces.py"


# ---- socket and directory naming -------------------------------------------------------------


def test_socket_paths_fit_in_the_unix_socket_limit() -> None:
    """AF_UNIX allows 108 bytes, and both of these are built from a workspace id that can be 129.
    Blowing the limit fails at bind time, in a workspace nobody would think to blame.

    Deliberately measured against the real $HOME rather than the temp one the rest of the suite
    runs against: it's the deployed path length that has to fit, and pytest's tmp dirs are not it.
    """
    longest = Workspace(project_id="p" * 64, name="w" * 64)
    digest = instances.socket_path(longest.id).name
    real_socket = Path("/tmp/workbench-vscode") / digest  # noqa: S108
    # code-server puts its own session socket inside the user-data-dir, so that path has to fit too.
    real_ipc = config.STATE_DIR / "vscode" / "instances" / Path(digest).stem / "code-server-ipc.sock"
    assert len(str(real_socket).encode()) < 108
    assert len(str(real_ipc).encode()) < 108


def test_each_workspace_gets_its_own_state_directory() -> None:
    """Sharing one user-data-dir between concurrent instances makes them collide over
    workspaceStorage and over the `code` CLI's socket."""
    assert instances.user_data_dir("proj/one") != instances.user_data_dir("proj/two")
    assert instances.user_data_dir("proj/one") == instances.user_data_dir("proj/one")


# ---- the instance cap ------------------------------------------------------------------------


def test_starting_a_third_editor_stops_the_least_recently_used(monkeypatch: pytest.MonkeyPatch) -> None:
    assert instances.MAX_INSTANCES == 2
    _fake_instance("proj/old", last_used=100.0)
    _fake_instance("proj/recent", last_used=500.0)

    stopped: list[str] = []

    async def fake_stop(workspace_id: str) -> bool:
        stopped.append(workspace_id)
        return instances._instances.pop(workspace_id, None) is not None

    monkeypatch.setattr(instances, "stop", fake_stop)
    asyncio.run(instances._evict_until_under_cap())

    assert stopped == ["proj/old"]
    assert [i.workspace_id for i in instances.running_instances()] == ["proj/recent"]


def test_using_an_editor_saves_it_from_being_evicted(monkeypatch: pytest.MonkeyPatch) -> None:
    """last_used tracks use, not start time: the one nobody has touched is the one that goes."""
    _fake_instance("proj/started-first", last_used=100.0)
    _fake_instance("proj/started-second", last_used=200.0)
    instances.touch("proj/started-first")

    stopped: list[str] = []

    async def fake_stop(workspace_id: str) -> bool:
        stopped.append(workspace_id)
        instances._instances.pop(workspace_id, None)
        return True

    monkeypatch.setattr(instances, "stop", fake_stop)
    asyncio.run(instances._evict_until_under_cap())

    assert stopped == ["proj/started-second"]


def test_an_exited_instance_is_not_counted_as_running() -> None:
    """code-server shuts itself down when idle, and nothing tells the workbench but the process."""
    _fake_instance("proj/gone", last_used=1.0, alive=False)
    assert instances.running("proj/gone") is None
    assert instances.running_instances() == []


# ---- shared config ---------------------------------------------------------------------------


def test_every_instance_shares_one_settings_file(workbench_home: Path) -> None:
    settings.ensure_shared_config()
    first = instances.user_data_dir("proj/one")
    second = instances.user_data_dir("proj/two")
    settings.link_shared_config(first)
    settings.link_shared_config(second)

    for data_dir in (first, second):
        link = data_dir / "User" / "settings.json"
        assert link.is_symlink()
        assert link.readlink() == settings.shared_settings_path()
    assert (first / "User" / "snippets").readlink() == paths.SHARED_USER_DIR / "snippets"


def test_linking_replaces_a_private_file_left_by_an_earlier_instance(workbench_home: Path) -> None:
    data_dir = instances.user_data_dir("proj/one")
    (data_dir / "User").mkdir(parents=True)
    (data_dir / "User" / "settings.json").write_text('{"editor.fontSize": 20}')
    (data_dir / "User" / "snippets").mkdir()

    settings.link_shared_config(data_dir)

    assert (data_dir / "User" / "settings.json").is_symlink()
    assert (data_dir / "User" / "snippets").is_symlink()


def test_the_defaults_are_only_written_once(workbench_home: Path) -> None:
    """They are seeds, not policy: whatever the user edits them into has to survive a restart."""
    settings.ensure_shared_config()
    settings.shared_settings_path().write_text('{"editor.fontSize": 20}')
    settings.ensure_shared_config()
    assert json.loads(settings.shared_settings_path().read_text()) == {"editor.fontSize": 20}


def test_copilot_is_off_by_default() -> None:
    """It spawns a ~200 MB language server per instance, signed out and unasked, in a 4 GB box."""
    assert settings.DEFAULT_SETTINGS["chat.disableAIFeatures"] is True


def test_every_workbench_theme_maps_to_an_editor_theme() -> None:
    assert set(settings.THEME_NAMES) == set(ui_settings.THEMES)


def test_changing_the_workbench_theme_recolours_the_editor(workbench_home: Path) -> None:
    settings.ensure_shared_config()
    with _client() as client:
        assert client.post("/api/ui/settings", json={"theme": "solarized-light"}).status_code == 200
    written = json.loads(settings.shared_settings_path().read_text())
    assert written["workbench.colorTheme"] == settings.THEME_NAMES["solarized-light"]


def test_a_settings_file_the_user_broke_is_left_alone(workbench_home: Path) -> None:
    """VS Code allows comments in settings.json and `json` does not, so an unreadable file here is
    the user's, not corruption. Saving a colour scheme must not rewrite it."""
    settings.ensure_shared_config()
    settings.shared_settings_path().write_text('{\n  // mine\n  "editor.fontSize": 20\n}')
    settings.apply_theme("solarized-dark")
    assert "// mine" in settings.shared_settings_path().read_text()


# ---- the API ---------------------------------------------------------------------------------


def test_the_proxy_says_so_when_no_editor_is_running(workbench_home: Path) -> None:
    workspaces.create_workspace_dir(Workspace(project_id="proj", name="main"))
    with _client() as client:
        response = client.get("/vscode/proj/main/")
    assert response.status_code == 503
    assert response.json()["error"] == "not_running"


def test_the_proxy_refuses_a_path_that_is_not_a_workspace(workbench_home: Path) -> None:
    with _client() as client:
        assert client.get("/vscode/nope").status_code == 404


def test_starting_an_editor_for_a_workspace_that_is_gone_is_a_404(workbench_home: Path) -> None:
    with _client() as client:
        response = client.post("/api/editor", json={"workspace_id": "proj/never-made"})
    assert response.status_code == 404


def test_starting_an_editor_needs_a_workspace_id(workbench_home: Path) -> None:
    with _client() as client:
        assert client.post("/api/editor", json={}).status_code == 400


def test_the_editor_list_reports_what_is_running(workbench_home: Path) -> None:
    _fake_instance("proj/main", last_used=1.0)
    with _client() as client:
        body = client.get("/api/editor").json()
    assert body["max_instances"] == 2
    assert [i["workspace_id"] for i in body["instances"]] == ["proj/main"]
    assert body["instances"][0]["url"] == "/vscode/proj/main/"


def test_starting_the_same_workspace_twice_reuses_one_instance(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two clicks, or a panel restored beside a page load, must not start two code-servers."""
    workspace = Workspace(project_id="proj", name="main")
    workspaces.create_workspace_dir(workspace)
    started = 0

    async def fake_spawn(*args: Any, **kwargs: Any) -> Any:
        nonlocal started
        started += 1
        await asyncio.sleep(0.05)
        proc = MagicMock()
        proc.returncode = None
        return proc

    async def fake_ready(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_install() -> Path:
        return workbench_home / "code-server"

    async def fake_extensions(binary: Path) -> None:
        return None

    async def never_reaped(instance: EditorInstance) -> None:
        return None

    monkeypatch.setattr(instances, "ensure_installed", fake_install)
    monkeypatch.setattr(instances, "ensure_default_extensions", fake_extensions)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(instances, "_wait_until_ready", fake_ready)
    monkeypatch.setattr(instances, "_reap", never_reaped)

    async def start_twice() -> list[EditorInstance]:
        return list(await asyncio.gather(instances.start(workspace), instances.start(workspace)))

    both = asyncio.run(start_twice())
    assert started == 1
    assert both[0] is both[1]


# ---- deleting a workspace --------------------------------------------------------------------


def test_deleting_a_workspace_stops_its_editor_and_drops_its_state(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(project_id="proj", name="main")
    workspaces.create_workspace_dir(workspace)
    data_dir = instances.user_data_dir(workspace.id)
    (data_dir / "User").mkdir(parents=True)

    stopped: list[str] = []

    async def fake_stop(workspace_id: str) -> bool:
        stopped.append(workspace_id)
        return True

    monkeypatch.setattr(instances, "stop", fake_stop)
    with _client() as client:
        assert client.delete("/api/workspaces/proj/main").status_code == 200

    assert stopped == ["proj/main"]
    assert not data_dir.exists()
    # The shared settings belong to every workspace, so they outlive any one of them.
    assert paths.SHARED_USER_DIR.exists() or not paths.SHARED_USER_DIR.parent.exists()


def test_terminate_refuses_a_pid_that_would_signal_the_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.killpg(1, ...) reaches tini, which forwards it: a bad pid here takes the whole workbench
    down instead of one editor."""
    monkeypatch.undo()  # this one wants the real _terminate
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 1
    with pytest.raises(ValueError, match="not an editor process"):
        asyncio.run(instances._terminate(proc))


def test_a_fresh_install_takes_the_workbench_s_current_theme(workbench_home: Path) -> None:
    """Seeding writes the default scheme, so a workbench already set to something else would hand
    the first editor the wrong colours."""
    ui_settings.save_ui_settings(ui_settings.UiSettings(theme="solarized-dark"))
    with _client():
        pass  # startup seeds the shared config and syncs the theme
    written = json.loads(settings.shared_settings_path().read_text())
    assert written["workbench.colorTheme"] == settings.THEME_NAMES["solarized-dark"]


# ---- the extensions a new workbench comes with -------------------------------------------------


def _fake_installer(monkeypatch: pytest.MonkeyPatch, attempted: list[str], *, succeeds: list[bool]) -> None:
    async def fake_install(binary: Path, extension: extensions.DefaultExtension) -> bool:
        attempted.append(extension.pinned)
        return succeeds[0]

    monkeypatch.setattr(extensions, "_install", fake_install)


def test_python_support_is_installed_without_being_asked_for() -> None:
    """A workspace is a repo the user cloned to work on, not an empty editor to set up first."""
    ids = [e.extension_id for e in extensions.DEFAULT_EXTENSIONS]
    assert "ms-python.python" in ids
    assert "meta.pyrefly" in ids
    assert "charliermarsh.ruff" in ids


def test_pylance_is_not_shipped_and_nothing_waits_for_it() -> None:
    """Pylance is licensed to Microsoft's own builds and is absent from Open VSX, so it cannot be
    installed here at all. Leaving python.languageServer at its default points the Python extension
    at it anyway, and it prompts about the missing install on every Python file."""
    assert "ms-python.vscode-pylance" not in [e.extension_id for e in extensions.DEFAULT_EXTENSIONS]
    assert settings.DEFAULT_SETTINGS["python.languageServer"] == "None"


def test_every_default_extension_is_pinned() -> None:
    """extensions.autoUpdate is off, so an unpinned install freezes on whatever was latest the day
    the container first started. The pin is what makes that a decision instead of a timestamp."""
    assert settings.DEFAULT_SETTINGS["extensions.autoUpdate"] is False
    for extension in extensions.DEFAULT_EXTENSIONS:
        assert extension.version
        assert extension.pinned == f"{extension.extension_id}@{extension.version}"


def test_the_python_formatter_is_one_of_the_extensions_that_gets_installed() -> None:
    """Naming a formatter that isn't installed fails on save with a dialog and no formatting."""
    python_settings = settings.DEFAULT_SETTINGS["[python]"]
    assert isinstance(python_settings, dict)
    formatter = python_settings["editor.defaultFormatter"]
    assert formatter in [e.extension_id for e in extensions.DEFAULT_EXTENSIONS]


def test_the_defaults_are_installed_once_and_not_again(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempted: list[str] = []
    _fake_installer(monkeypatch, attempted, succeeds=[True])
    binary = workbench_home / "code-server"

    asyncio.run(extensions.ensure_default_extensions(binary))
    assert attempted == [e.pinned for e in extensions.DEFAULT_EXTENSIONS]

    attempted.clear()
    asyncio.run(extensions.ensure_default_extensions(binary))
    assert attempted == []


def test_an_extension_that_could_not_be_installed_is_tried_again(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open VSX being unreachable for one start must not cost the workspace Python support for
    good, so only what really installed is written down."""
    attempted: list[str] = []
    succeeds = [False]
    _fake_installer(monkeypatch, attempted, succeeds=succeeds)
    binary = workbench_home / "code-server"

    asyncio.run(extensions.ensure_default_extensions(binary))
    assert attempted == [e.pinned for e in extensions.DEFAULT_EXTENSIONS]
    assert extensions.already_installed() == set()

    attempted.clear()
    succeeds[0] = True
    asyncio.run(extensions.ensure_default_extensions(binary))
    assert attempted == [e.pinned for e in extensions.DEFAULT_EXTENSIONS]


def test_an_extension_the_user_uninstalled_is_not_forced_back(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What has been installed before is read off the record, never off the extensions directory.
    Probing the directory would undo the Extensions panel's uninstall button on the next start."""
    marker = extensions.installed_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps([e.pinned for e in extensions.DEFAULT_EXTENSIONS]))

    attempted: list[str] = []
    _fake_installer(monkeypatch, attempted, succeeds=[True])
    asyncio.run(extensions.ensure_default_extensions(workbench_home / "code-server"))

    assert attempted == []
    assert not paths.EXTENSIONS_DIR.exists()


def test_bumping_a_pin_installs_the_new_version(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The record is keyed by version, so editing DEFAULT_EXTENSIONS is all an upgrade takes."""
    marker = extensions.installed_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps([f"{e.extension_id}@0.0.0" for e in extensions.DEFAULT_EXTENSIONS]))

    attempted: list[str] = []
    _fake_installer(monkeypatch, attempted, succeeds=[True])
    asyncio.run(extensions.ensure_default_extensions(workbench_home / "code-server"))

    assert attempted == [e.pinned for e in extensions.DEFAULT_EXTENSIONS]


def test_a_marker_file_that_is_not_readable_does_not_stop_an_editor_starting(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = extensions.installed_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{ not json")

    attempted: list[str] = []
    _fake_installer(monkeypatch, attempted, succeeds=[True])
    asyncio.run(extensions.ensure_default_extensions(workbench_home / "code-server"))

    assert attempted == [e.pinned for e in extensions.DEFAULT_EXTENSIONS]
