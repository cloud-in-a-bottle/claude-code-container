from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server import claude_trust
from server.claude_trust import is_trusted
from server.claude_trust import trust_dir

WS = "/workspaces/proj/ws"


def _config() -> Path:
    return Path(claude_trust.CLAUDE_CONFIG_PATH)


def _write_config(payload: object) -> None:
    path = _config()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)


def _read_config() -> dict[str, Any]:
    config: dict[str, Any] = json.loads(_config().read_text())
    return config


def test_a_directory_starts_untrusted(workbench_home: Path) -> None:
    assert not is_trusted(WS)


def test_trusting_a_directory_sticks(workbench_home: Path) -> None:
    trust_dir(WS)
    assert is_trusted(WS)
    assert _read_config()["projects"] == {WS: {"hasTrustDialogAccepted": True}}


def test_trust_is_per_directory(workbench_home: Path) -> None:
    trust_dir(WS)
    assert not is_trusted("/workspaces/proj/other")


def test_a_path_object_and_its_string_are_the_same_directory(workbench_home: Path) -> None:
    trust_dir(Path(WS))
    assert is_trusted(WS)


def test_trusting_keeps_what_claude_already_recorded(workbench_home: Path) -> None:
    """Claude keeps history and counters under the same key; trusting must not be a reset."""
    _write_config({"projects": {WS: {"lastSessionId": "abc", "lastCost": 3}}})
    trust_dir(WS)
    assert _read_config()["projects"] == {WS: {"lastSessionId": "abc", "lastCost": 3, "hasTrustDialogAccepted": True}}


def test_trusting_keeps_the_rest_of_the_config(workbench_home: Path) -> None:
    """The same file holds the user's login. Losing it to skip a dialog is not a trade worth making."""
    _write_config({"oauthAccount": {"emailAddress": "someone@example.com"}, "numStartups": 12})
    trust_dir(WS)
    config = _read_config()
    assert config["oauthAccount"] == {"emailAddress": "someone@example.com"}
    assert config["numStartups"] == 12


def test_an_already_trusted_directory_is_not_rewritten(workbench_home: Path) -> None:
    """Claude rewrites this file from every running tab, so every avoidable write is a lost update.

    Once the flag is set there is nothing to add, and the steady state is no writes at all.
    """
    trust_dir(WS)
    before = _config().stat().st_mtime_ns
    _config().write_text(_config().read_text())  # same content, new mtime
    touched = _config().stat().st_mtime_ns
    trust_dir(WS)
    assert _config().stat().st_mtime_ns == touched != before


def test_a_missing_config_is_created(workbench_home: Path) -> None:
    assert not _config().exists()
    trust_dir(WS)
    assert is_trusted(WS)


def test_an_unparseable_config_is_left_alone(workbench_home: Path) -> None:
    """Overwriting it would log the user out; the dialog they'd see instead is the smaller loss."""
    _write_config("{ this is not json")
    trust_dir(WS)
    assert _config().read_text() == "{ this is not json"


@pytest.mark.parametrize("payload", [[], "null", '"a string"', "42"])
def test_a_config_that_is_not_an_object_is_left_alone(workbench_home: Path, payload: object) -> None:
    _write_config(payload)
    original = _config().read_text()
    trust_dir(WS)
    assert _config().read_text() == original
    assert not is_trusted(WS)


@pytest.mark.parametrize("projects", [[], "not-a-map", 7])
def test_a_projects_key_of_the_wrong_shape_is_left_alone(workbench_home: Path, projects: object) -> None:
    _write_config({"projects": projects})
    original = _config().read_text()
    trust_dir(WS)
    assert _config().read_text() == original


def test_an_entry_of_the_wrong_shape_is_replaced_rather_than_merged(workbench_home: Path) -> None:
    """Nothing to preserve in a non-object entry, and it would otherwise block the flag for good."""
    _write_config({"projects": {WS: "unexpected"}})
    trust_dir(WS)
    assert is_trusted(WS)


def test_a_write_that_fails_does_not_take_the_tab_down(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal that opens on the trust dialog still beats no terminal."""

    def unwritable(self: Path, *args: object, **kwargs: object) -> object:
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "write_text", unwritable)
    trust_dir(WS)  # must not raise
    assert not is_trusted(WS)


def test_no_temp_file_is_left_behind_by_a_failed_write(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config({"projects": {}})

    def fail_rename(self: Path, *args: object, **kwargs: object) -> object:
        raise OSError("rename failed")

    monkeypatch.setattr(Path, "replace", fail_rename)
    trust_dir(WS)
    assert list(_config().parent.glob("*.workbench.tmp")) == []
