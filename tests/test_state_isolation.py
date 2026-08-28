"""The suite is routinely run from inside a live workbench, against the $HOME it persists to.

A test that reaches the real state files there doesn't fail — it deletes the user's session and
passes. So the isolation itself gets tested, and every persisted path is enumerated in one place
that a new one has to be added to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import _STATE_PATHS

from server import config


def _live_paths() -> list[Path]:
    return [Path(getattr(module, attribute)) for module, attribute, _ in _STATE_PATHS]


def test_no_persisted_path_points_into_the_real_home() -> None:
    """This test asks for no fixture on purpose: the guard has to hold without being opted into."""
    for path in _live_paths():
        assert not path.is_relative_to(config.HOME), f"{path} would be written inside the real $HOME"


def test_every_persisted_path_is_redirected_together(workbench_home: Path) -> None:
    for path in _live_paths():
        assert path.is_relative_to(workbench_home), f"{path} escaped the temp home"


def test_each_test_gets_its_own_state(workbench_home: Path) -> None:
    """Paired with the next test: two tests must not be handed the same directory."""
    (workbench_home / "marker").write_text("first")


def test_state_does_not_leak_between_tests(workbench_home: Path) -> None:
    assert not (workbench_home / "marker").exists()


@pytest.mark.parametrize("attribute", [attribute for _, attribute, _ in _STATE_PATHS])
def test_the_redirect_covers_the_module_attribute_the_server_actually_reads(
    workbench_home: Path, attribute: str
) -> None:
    """Guards against a rename: patching a name the server no longer reads silently does nothing."""
    module = next(m for m, a, _ in _STATE_PATHS if a == attribute)
    assert hasattr(module, attribute)
