"""Which account a workspace's Claude sessions are billed to, and how that reaches a process.

The failure this file guards against is silent: a workspace that says "subscription" in the UI but
whose terminals inherit an ANTHROPIC_API_KEY bills the API account and nothing on screen says so.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from server import app as srv
from server import billing
from server import tabs as tabs_module
from server.billing import API
from server.billing import SUBSCRIPTION
from server.billing import Billing
from server.routes import settings as settings_routes


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


@pytest.fixture(autouse=True)
def no_secrets_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No unit test should reach the secrets app. Tests that care stub a key back."""
    _stub_key(monkeypatch, "")


def _stub_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    async def fake() -> str:
        return key

    monkeypatch.setattr(billing, "get_anthropic_key", fake)


def _stub_subscription(monkeypatch: pytest.MonkeyPatch, *, logged_in: bool) -> None:
    """Stub the `claude auth status` probe, which otherwise shells out to the real CLI."""

    async def fake() -> dict[str, Any]:
        return {"logged_in": logged_in, "method": "claudeai" if logged_in else "none", "account": "", "detail": ""}

    monkeypatch.setattr(billing, "subscription_status", fake)
    # The route imported the name, so that binding is the one it reads.
    monkeypatch.setattr(settings_routes, "subscription_status", fake)


# ── persistence ────────────────────────────────────────────────────────────────


def test_defaults_to_api_billing_when_nothing_is_saved() -> None:
    assert billing.load_billing() == Billing(default_mode=API, workspaces={})


def test_save_then_load_round_trips() -> None:
    billing.save_billing(Billing(default_mode=SUBSCRIPTION, workspaces={"r/w": API}))
    assert billing.load_billing() == Billing(default_mode=SUBSCRIPTION, workspaces={"r/w": API})


def test_save_creates_its_parent_dir_and_leaves_no_temp_file(workbench_home: Path) -> None:
    path = workbench_home / ".workbench" / "billing.json"
    assert not path.parent.exists()
    billing.save_billing(Billing(default_mode=SUBSCRIPTION))
    assert [p.name for p in path.parent.iterdir()] == ["billing.json"]


def test_a_file_from_an_older_build_still_loads(workbench_home: Path) -> None:
    path = workbench_home / ".workbench" / "billing.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"default_mode": SUBSCRIPTION, "some_future_setting": 7}))
    assert billing.load_billing() == Billing(default_mode=SUBSCRIPTION, workspaces={})


@pytest.mark.parametrize(
    "contents",
    [
        "{not json",
        json.dumps([SUBSCRIPTION]),
        json.dumps({"default_mode": "free"}),
        json.dumps({"workspaces": {"r/w": "free"}}),
        json.dumps({"workspaces": "r/w"}),
    ],
)
def test_a_file_that_cannot_be_trusted_raises_rather_than_guessing(workbench_home: Path, contents: str) -> None:
    """Falling back to a default here would quietly bill the wrong account."""
    path = workbench_home / ".workbench" / "billing.json"
    path.parent.mkdir(parents=True)
    path.write_text(contents)
    with pytest.raises(ValueError):
        billing.load_billing()


def test_saving_an_unknown_mode_raises() -> None:
    with pytest.raises(ValueError):
        billing.save_billing(Billing(default_mode="free"))
    with pytest.raises(ValueError):
        billing.save_billing(Billing(workspaces={"r/w": "free"}))


# ── which mode a workspace is on ───────────────────────────────────────────────


def test_a_workspace_uses_the_mode_it_was_created_with() -> None:
    billing.pin_workspace_mode("r/w", SUBSCRIPTION)
    assert billing.workspace_mode("r/w") == SUBSCRIPTION


def test_a_workspace_with_no_entry_follows_the_current_default() -> None:
    """Workspaces that predate this setting, and ones edited out of the file by hand."""
    assert billing.workspace_mode("r/old") == API
    billing.set_default_mode(SUBSCRIPTION)
    assert billing.workspace_mode("r/old") == SUBSCRIPTION


def test_changing_the_default_leaves_existing_workspaces_alone() -> None:
    """A workspace holds work and conversations; moving it to another account behind the user's
    back is exactly what pinning at creation is for."""
    billing.pin_workspace_mode("r/api", API)
    billing.pin_workspace_mode("r/sub", SUBSCRIPTION)

    billing.set_default_mode(SUBSCRIPTION)
    assert billing.workspace_mode("r/api") == API
    assert billing.workspace_mode("r/sub") == SUBSCRIPTION

    billing.set_default_mode(API)
    assert billing.workspace_mode("r/sub") == SUBSCRIPTION


def test_pinning_one_workspace_keeps_the_others() -> None:
    billing.pin_workspace_mode("r/one", SUBSCRIPTION)
    billing.pin_workspace_mode("r/two", API)
    assert billing.load_billing().workspaces == {"r/one": SUBSCRIPTION, "r/two": API}


def test_forgetting_a_workspace_drops_only_its_entry() -> None:
    billing.pin_workspace_mode("r/one", SUBSCRIPTION)
    billing.pin_workspace_mode("r/two", SUBSCRIPTION)
    billing.forget_workspace("r/one")
    assert billing.load_billing().workspaces == {"r/two": SUBSCRIPTION}
    # Twice, and for a workspace that was never pinned, because deletes are not always tidy.
    billing.forget_workspace("r/one")
    billing.forget_workspace("r/never")
    assert billing.load_billing().workspaces == {"r/two": SUBSCRIPTION}


# ── the environment a child process gets ───────────────────────────────────────


def test_api_billing_passes_the_key_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_key(monkeypatch, "sk-test")
    assert asyncio.run(billing.auth_env(API)) == {"ANTHROPIC_API_KEY": "sk-test"}


def test_api_billing_with_no_key_from_secrets_leaves_the_environment_as_it_is() -> None:
    """An empty overlay, not an unset: a key exported by hand in the container still works."""
    assert asyncio.run(billing.auth_env(API)) == {}


def test_subscription_billing_removes_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not merely "doesn't add one": Claude Code prefers a key over the stored OAuth credentials,
    so an inherited key would bill the API account while the UI said subscription."""
    _stub_key(monkeypatch, "sk-test")
    assert asyncio.run(billing.auth_env(SUBSCRIPTION)) == {"ANTHROPIC_API_KEY": None}


def test_the_workspace_decides_which_auth_its_processes_get(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_key(monkeypatch, "sk-test")
    billing.pin_workspace_mode("r/sub", SUBSCRIPTION)
    billing.pin_workspace_mode("r/api", API)
    assert asyncio.run(billing.workspace_auth_env("r/sub")) == {"ANTHROPIC_API_KEY": None}
    assert asyncio.run(billing.workspace_auth_env("r/api")) == {"ANTHROPIC_API_KEY": "sk-test"}


def test_an_overlay_sets_replaces_and_removes() -> None:
    env = {"KEEP": "1", "REPLACE": "old", "GONE": "x"}
    billing.apply_env_overlay(env, {"REPLACE": "new", "GONE": None, "ADDED": "2", "NEVER_THERE": None})
    assert env == {"KEEP": "1", "REPLACE": "new", "ADDED": "2"}


def test_a_subscription_tab_really_starts_without_the_key(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted on a spawned process, not the overlay: what matters is what `claude` inherits.

    The key is put in the server's own environment first, because that is the case the overlay
    exists for — openhost can inject one, and a plain "don't set it" would not shed it.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-inherited")
    billing.pin_workspace_mode("r/sub", SUBSCRIPTION)

    async def spawn_and_read() -> str:
        tab = await tabs_module.create_server_tab(
            command=["bash", "-c", "printenv ANTHROPIC_API_KEY; echo done"],
            cwd=str(workbench_home),
            env=await billing.workspace_auth_env("r/sub"),
            label="t",
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b"done" not in bytes(tab.output_buf):
            await asyncio.sleep(0.05)
        tabs_module.kill_tab(tab)
        return bytes(tab.output_buf).decode(errors="replace")

    output = asyncio.run(spawn_and_read())
    assert "sk-inherited" not in output
    assert "done" in output


def test_an_api_tab_starts_with_the_key(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_key(monkeypatch, "sk-test")
    billing.pin_workspace_mode("r/api", API)

    async def spawn_and_read() -> str:
        tab = await tabs_module.create_server_tab(
            command=["bash", "-c", "printenv ANTHROPIC_API_KEY"],
            cwd=str(workbench_home),
            env=await billing.workspace_auth_env("r/api"),
            label="t",
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not tab.output_buf:
            await asyncio.sleep(0.05)
        tabs_module.kill_tab(tab)
        return bytes(tab.output_buf).decode(errors="replace")

    assert asyncio.run(spawn_and_read()).strip() == "sk-test"


# ── reading `claude auth status` ───────────────────────────────────────────────


def test_a_subscription_login_reads_as_signed_in() -> None:
    status = billing.parse_auth_status(json.dumps({"loggedIn": True, "authMethod": "claudeai"}).encode())
    assert status["logged_in"] is True
    assert status["method"] == "claudeai"


def test_no_login_reads_as_not_signed_in() -> None:
    status = billing.parse_auth_status(json.dumps({"loggedIn": False, "authMethod": "none"}).encode())
    assert status["logged_in"] is False


def test_a_key_answer_is_not_a_subscription() -> None:
    """The probe strips ANTHROPIC_API_KEY, so a key answer means one is arriving another way —
    an apiKeyHelper, say. That is still not something a subscription workspace can use."""
    status = billing.parse_auth_status(
        json.dumps({"loggedIn": True, "authMethod": "api_key", "apiKeySource": "helper"}).encode()
    )
    assert status["logged_in"] is False


def test_an_account_is_reported_when_the_cli_volunteers_one() -> None:
    status = billing.parse_auth_status(json.dumps({"loggedIn": True, "email": "a@b.co"}).encode())
    assert status["account"] == "a@b.co"


@pytest.mark.parametrize("stdout", [b"", b"not json", b'"a string"', b"[]"])
def test_output_that_cannot_be_read_becomes_not_signed_in_with_an_explanation(stdout: bytes) -> None:
    status = billing.parse_auth_status(stdout, b"claude: command failed")
    assert status["logged_in"] is False
    assert status["detail"]


# ── HTTP API ───────────────────────────────────────────────────────────────────


def test_the_settings_endpoint_reports_the_default_and_both_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_key(monkeypatch, "sk-test")
    _stub_subscription(monkeypatch, logged_in=True)
    body = _client().get("/api/settings").json()
    assert body["default_billing"] == API
    assert body["api_key_available"] is True
    assert body["subscription"]["logged_in"] is True


def test_missing_credentials_are_reported_rather_than_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page's whole job is to say what still needs setting up."""
    _stub_subscription(monkeypatch, logged_in=False)
    body = _client().get("/api/settings").json()
    assert body["api_key_available"] is False
    assert body["subscription"]["logged_in"] is False


def test_the_default_can_be_changed_and_sticks(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_subscription(monkeypatch, logged_in=True)
    client = _client()
    resp = client.post("/api/settings", json={"default_billing": SUBSCRIPTION})
    assert resp.status_code == 200
    assert resp.json()["default_billing"] == SUBSCRIPTION
    assert client.get("/api/settings").json()["default_billing"] == SUBSCRIPTION
    assert billing.load_billing().default_mode == SUBSCRIPTION


def test_an_unknown_default_is_rejected_and_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_subscription(monkeypatch, logged_in=False)
    client = _client()
    client.post("/api/settings", json={"default_billing": SUBSCRIPTION})
    assert client.post("/api/settings", json={"default_billing": "free"}).status_code == 400
    assert client.post("/api/settings", json={}).status_code == 400
    assert billing.load_billing().default_mode == SUBSCRIPTION
