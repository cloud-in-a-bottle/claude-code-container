from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

import pytest

from server import claude_sessions
from server import remote_services
from server import tab_store
from server import tabs
from server import ui_settings
from server.editor import paths as editor_paths
from server.projects import store
from server.projects import workspaces

if TYPE_CHECKING:
    from openhost_test_harness import OpenhostStack


@pytest.fixture(scope="session")
def stack() -> Iterator[OpenhostStack]:
    """Build the app's Dockerfile, run it under podman per openhost.toml, and
    front it with the real OpenHost router.

    OpenhostStack() finds openhost.toml by walking up from the cwd, so no app_dir
    is needed as long as tests run from within the app tree.

    - stack.url                   — through the router; requires owner auth
    - stack.owner_session         — a requests.Session authenticated as the zone owner
    - stack.playwright_login(page) — log a playwright page in as the owner for browser tests
    - stack.app_url               — direct to the container (control your own headers; eg the health probe)

    Imported lazily: the unit tests must still collect on a machine without the
    harness (or without podman), and `just test` deselects everything that uses this.
    """
    harness: Any = pytest.importorskip(
        "openhost_test_harness", reason="openhost[test-harness] is not installed; run `just setup`"
    )
    with harness.OpenhostStack() as s:
        yield s


# Every module-level path the server writes to, and where each is redirected under a temp home.
# Anything added here must also be added to _STATE_PATHS below or the guard misses it.
_STATE_PATHS: tuple[tuple[Any, str, str], ...] = (
    (store, "PROJECTS_PATH", ".workbench/projects.json"),
    (workspaces, "WORKSPACES_ROOT", "workspaces"),
    (workspaces, "MIRRORS_DIR", ".workbench/mirrors"),
    (tab_store, "TABS_PATH", ".workbench/tabs.json"),
    (ui_settings, "UI_SETTINGS_PATH", ".workbench/ui.json"),
    (claude_sessions, "CLAUDE_PROJECTS_DIR", ".claude/projects"),
    (remote_services, "GH_HOSTS_PATH", ".config/gh/hosts.yml"),
    (remote_services, "GH_MANAGED_MARKER", ".workbench/gh-auth-managed"),
    (remote_services, "GIT_CONFIG_PATH", ".gitconfig"),
    (editor_paths, "VSCODE_DIR", ".workbench/vscode"),
    (editor_paths, "INSTALL_DIR", ".workbench/vscode/install"),
    (editor_paths, "SHARED_USER_DIR", ".workbench/vscode/user"),
    (editor_paths, "EXTENSIONS_DIR", ".workbench/vscode/extensions"),
    (editor_paths, "INSTANCES_DIR", ".workbench/vscode/instances"),
    (editor_paths, "CONFIG_PATH", ".workbench/vscode/config.yaml"),
    # Not $HOME state — sockets live in /tmp — but redirected all the same, so a test can't reach
    # the socket a running workbench is serving its editors on.
    (editor_paths, "SOCKETS_DIR", "vscode-sockets"),
)


def _redirect_state(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for module, attribute, relative in _STATE_PATHS:
        monkeypatch.setattr(module, attribute, root / relative)
    # persist_tabs() skips a write whose snapshot matches this, so a value left over from an
    # earlier test would suppress the first write against the new path.
    monkeypatch.setattr(tabs, "_last_persisted", [])


@pytest.fixture(autouse=True)
def _isolate_workbench_state(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the real $HOME, whether or not it asked to be kept off it.

    This is autouse rather than opt-in because of how the workbench gets used: it is its own
    dogfood, so `just test` routinely runs *inside* a live workbench, against the same $HOME the
    running server is persisting to. A test that reaches the real state files there does not fail
    loudly -- it quietly destroys the user's session. That is not hypothetical. Before this
    existed, `test_delete_workspace_kills_its_tabs` went through the real delete route into
    `persist_tabs()` and overwrote `~/.workbench/tabs.json` with its own fixture tabs, so the next
    restart restored nothing and every running Claude conversation was orphaned:

        [tabs] dropping tab 'claude': workspace 'r/keep' is gone
        [tabs] restored 0 tab(s) from the previous run

    Opting in per test is the wrong shape for that failure mode. Forgetting is silent, and the
    damage lands on the user rather than on the test.
    """
    _redirect_state(tmp_path_factory.mktemp("workbench-state"), monkeypatch)


@pytest.fixture
def workbench_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every piece of on-disk workbench state at a temp dir, so tests never touch $HOME.

    `_isolate_workbench_state` already guarantees the safety half of this; what this adds is a
    *known* directory the test can then make assertions about.
    """
    _redirect_state(tmp_path, monkeypatch)
    return tmp_path
