from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

import server
import ui_settings
from ui_settings import UiSettings

_T = TypeVar("_T")


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def settings_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the settings file at a temp dir so tests never touch the real $HOME."""
    path = tmp_path / ".workbench" / "ui.json"
    monkeypatch.setattr(ui_settings, "UI_SETTINGS_PATH", path)
    return path


# ── persistence ────────────────────────────────────────────────────────────────


def test_defaults_to_off_when_unset() -> None:
    assert ui_settings.load_ui_settings() == UiSettings(side_panel=False)


def test_save_then_load_round_trips(settings_path: Path) -> None:
    ui_settings.save_ui_settings(UiSettings(side_panel=True))
    assert settings_path.exists()
    assert ui_settings.load_ui_settings() == UiSettings(side_panel=True)

    ui_settings.save_ui_settings(UiSettings(side_panel=False))
    assert ui_settings.load_ui_settings() == UiSettings(side_panel=False)


def test_save_creates_parent_dir(settings_path: Path) -> None:
    assert not settings_path.parent.exists()
    ui_settings.save_ui_settings(UiSettings(side_panel=True))
    assert settings_path.parent.is_dir()


def test_save_leaves_no_temp_file_behind(settings_path: Path) -> None:
    ui_settings.save_ui_settings(UiSettings(side_panel=True))
    assert [p.name for p in settings_path.parent.iterdir()] == ["ui.json"]


def test_unknown_keys_are_ignored(settings_path: Path) -> None:
    """A file written by a newer build must not break an older one."""
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"side_panel": True, "some_future_setting": 7}))
    assert ui_settings.load_ui_settings() == UiSettings(side_panel=True)


def test_missing_key_falls_back_to_default(settings_path: Path) -> None:
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"some_future_setting": 7}))
    assert ui_settings.load_ui_settings() == UiSettings(side_panel=False)


def test_malformed_file_raises(settings_path: Path) -> None:
    """Loud beats a toggle that silently forgets what the user set."""
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        ui_settings.load_ui_settings()


# ── HTTP API ───────────────────────────────────────────────────────────────────


def test_get_settings_reports_default() -> None:
    async def go() -> None:
        client = server.app.test_client()
        resp = await client.get("/api/ui/settings")
        assert resp.status_code == 200
        assert await resp.get_json() == {"side_panel": False, "theme": "dark"}

    run(go())


def test_post_settings_persists(settings_path: Path) -> None:
    async def go() -> None:
        client = server.app.test_client()
        resp = await client.post("/api/ui/settings", json={"side_panel": True})
        assert resp.status_code == 200
        assert await resp.get_json() == {"side_panel": True, "theme": "dark"}

        resp = await client.get("/api/ui/settings")
        assert await resp.get_json() == {"side_panel": True, "theme": "dark"}

    run(go())
    assert ui_settings.load_ui_settings() == UiSettings(side_panel=True)


def test_post_without_any_known_key_is_400() -> None:
    async def go() -> None:
        client = server.app.test_client()
        assert (await client.post("/api/ui/settings", json={})).status_code == 400
        assert (await client.post("/api/ui/settings", json={"nope": 1})).status_code == 400

    run(go())


# ── themes ─────────────────────────────────────────────────────────────────────


def test_theme_defaults_to_dark() -> None:
    assert ui_settings.load_ui_settings().theme == ui_settings.DEFAULT_THEME


def test_solarized_light_round_trips() -> None:
    ui_settings.save_ui_settings(UiSettings(theme="solarized-light"))
    assert ui_settings.load_ui_settings().theme == "solarized-light"


def test_saving_unknown_theme_raises() -> None:
    with pytest.raises(ValueError):
        ui_settings.save_ui_settings(UiSettings(theme="chartreuse"))


def test_loading_unknown_theme_raises(settings_path: Path) -> None:
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"theme": "chartreuse"}))
    with pytest.raises(ValueError):
        ui_settings.load_ui_settings()


def test_post_theme_persists() -> None:
    async def go() -> None:
        client = server.app.test_client()
        resp = await client.post("/api/ui/settings", json={"theme": "solarized-light"})
        assert resp.status_code == 200
        assert (await resp.get_json())["theme"] == "solarized-light"

    run(go())
    assert ui_settings.load_ui_settings().theme == "solarized-light"


def test_post_unknown_theme_is_400_and_changes_nothing() -> None:
    async def go() -> None:
        client = server.app.test_client()
        await client.post("/api/ui/settings", json={"theme": "solarized-dark"})
        resp = await client.post("/api/ui/settings", json={"theme": "chartreuse"})
        assert resp.status_code == 400

    run(go())
    assert ui_settings.load_ui_settings().theme == "solarized-dark"


def test_updating_one_setting_leaves_the_other_alone() -> None:
    """The picker POSTs only `theme`; the skill POSTs only `side_panel`. Neither may clobber."""

    async def go() -> None:
        client = server.app.test_client()
        await client.post("/api/ui/settings", json={"side_panel": True})
        await client.post("/api/ui/settings", json={"theme": "solarized-light"})
        body = await (await client.get("/api/ui/settings")).get_json()
        assert body == {"side_panel": True, "theme": "solarized-light"}

        await client.post("/api/ui/settings", json={"side_panel": False})
        body = await (await client.get("/api/ui/settings")).get_json()
        assert body == {"side_panel": False, "theme": "solarized-light"}

    run(go())


def test_theme_names_match_the_client() -> None:
    """The theme list is duplicated in Python, JS and CSS; a comment alone wouldn't keep them honest.

    A name the server accepts but the client can't paint would silently render an unstyled UI.
    """
    here = Path(__file__).parent

    js = (here / "static" / "theme.js").read_text()
    # keys of the THEMES map, e.g.   'solarized-light': {
    js_names = set(re.findall(r"^\s*'([a-z0-9-]+)':\s*\{$", js, re.MULTILINE))
    assert js_names == set(ui_settings.THEMES)

    css = (here / "static" / "themes.css").read_text()
    css_names = set(re.findall(r'\[data-theme="([a-z0-9-]+)"\]', css))
    # the default theme is the bare :root block rather than an attribute selector
    assert css_names == set(ui_settings.THEMES) - {ui_settings.DEFAULT_THEME}


def test_index_carries_the_theme_for_a_flash_free_load() -> None:
    async def go() -> None:
        client = server.app.test_client()
        body = await (await client.get("/")).get_data(as_text=True)
        assert 'data-theme="dark"' in body

        await client.post("/api/ui/settings", json={"theme": "solarized-light"})
        body = await (await client.get("/")).get_data(as_text=True)
        assert 'data-theme="solarized-light"' in body
        assert "/static/theme.js" in body

    run(go())


# ── the toggle actually changes the page ───────────────────────────────────────


def test_index_omits_panel_script_by_default() -> None:
    async def go() -> None:
        client = server.app.test_client()
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "side-panel.js" not in (await resp.get_data(as_text=True))

    run(go())


def test_index_includes_panel_script_when_enabled() -> None:
    async def go() -> None:
        client = server.app.test_client()
        await client.post("/api/ui/settings", json={"side_panel": True})
        resp = await client.get("/")
        body = await resp.get_data(as_text=True)
        assert "side-panel.js" in body
        # the terminal UI must still be intact
        assert "app.js" in body
        assert 'id="panes"' in body

    run(go())
