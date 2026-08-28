from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

import pytest

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


@pytest.fixture
def workbench_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every piece of on-disk workbench state at a temp dir, so tests never touch $HOME."""
    monkeypatch.setattr(store, "PROJECTS_PATH", tmp_path / ".workbench" / "projects.json")
    monkeypatch.setattr(workspaces, "WORKSPACES_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr(workspaces, "MIRRORS_DIR", tmp_path / ".workbench" / "mirrors")
    return tmp_path
