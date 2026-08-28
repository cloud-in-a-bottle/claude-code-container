"""The gh auto-login path: minting a token from the oauth app and handing it to the gh CLI.

The two failures these cover are the ones that silently left gh logged out: minting without an
`account` (always `permission_required`), and `gh auth login --with-token` rejecting a
`repo`-scoped token for want of `read:org`.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Coroutine
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from server import remote_services


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _forget_cached_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_services, "_github_account", None)


def fake_oauth(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, tuple[int, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Stub the oauth app, recording every payload it is called with."""
    calls: list[dict[str, Any]] = []

    async def _call(endpoint: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        calls.append({"endpoint": endpoint, **payload})
        return responses.get(endpoint, (403, {}))

    monkeypatch.setattr(remote_services, "_call_oauth", _call)
    return calls


# ── minting ────────────────────────────────────────────────────────────────────


def test_mint_names_the_granted_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this exists for: without an explicit account the call always returns 403."""
    calls = fake_oauth(
        monkeypatch,
        {
            "accounts": (200, {"accounts": ["octocat"]}),
            "token": (200, {"access_token": "ghs_abc", "token_type": "Bearer"}),
        },
    )
    minted = run(remote_services.mint_github_token())
    assert minted is not None
    assert minted.token == "ghs_abc"
    assert minted.account == "octocat"
    assert [c["endpoint"] for c in calls] == ["accounts", "token"]
    assert calls[1]["account"] == "octocat"


def test_mint_falls_back_to_default_when_accounts_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`default` still resolves server-side when exactly one account is connected."""
    calls = fake_oauth(
        monkeypatch,
        {"accounts": (403, {}), "token": (200, {"access_token": "ghs_abc"})},
    )
    minted = run(remote_services.mint_github_token())
    assert minted is not None
    assert calls[1]["account"] == "default"


def test_mint_reuses_the_resolved_account(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_oauth(
        monkeypatch,
        {"accounts": (200, {"accounts": ["octocat"]}), "token": (200, {"access_token": "ghs_abc"})},
    )
    run(remote_services.mint_github_token())
    run(remote_services.mint_github_token())
    assert [c["endpoint"] for c in calls] == ["accounts", "token", "token"]


def test_mint_reresolves_the_account_after_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked or renamed account must not be cached forever."""
    calls = fake_oauth(
        monkeypatch,
        {"accounts": (200, {"accounts": ["octocat"]}), "token": (401, {})},
    )
    assert run(remote_services.mint_github_token()) is None
    run(remote_services.mint_github_token())
    assert [c["endpoint"] for c in calls] == ["accounts", "token", "accounts", "token"]


def test_accounts_are_deduped_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_oauth(monkeypatch, {"accounts": (200, {"accounts": ["b", "a", "b"]})})
    assert run(remote_services.fetch_github_accounts()) == ["b", "a"]


def test_mint_returns_none_on_an_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_oauth(monkeypatch, {"accounts": (200, {"accounts": ["o"]}), "token": (200, {"access_token": "  "})})
    assert run(remote_services.mint_github_token()) is None


def test_fetch_github_token_yields_the_bare_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_oauth(monkeypatch, {"accounts": (200, {"accounts": ["o"]}), "token": (200, {"access_token": "ghs_abc"})})
    assert run(remote_services.fetch_github_token()) == "ghs_abc"


def test_fetch_github_token_is_empty_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_oauth(monkeypatch, {"accounts": (403, {}), "token": (403, {})})
    assert run(remote_services.fetch_github_token()) == ""


# ── expiry ─────────────────────────────────────────────────────────────────────


def test_expiry_drives_the_refresh_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    expires = datetime.now(UTC) + timedelta(seconds=900)
    fake_oauth(
        monkeypatch,
        {
            "accounts": (200, {"accounts": ["o"]}),
            "token": (200, {"access_token": "ghs_abc", "expires_at": expires.isoformat()}),
        },
    )
    minted = run(remote_services.mint_github_token())
    assert minted is not None
    # 900s out, minus the 300s safety margin, give or take the time the call took.
    assert 590 <= minted.seconds_until_refresh() <= 600


@pytest.mark.parametrize("raw", [None, "", "not-a-date"])
def test_a_missing_or_unparseable_expiry_falls_back_to_the_default_cadence(raw: object) -> None:
    token = remote_services.GithubToken(token="t", expires_at=remote_services._parse_expiry(raw))
    assert token.seconds_until_refresh() == remote_services.GH_REFRESH_MAX_SECONDS


def test_a_naive_expiry_is_read_as_utc() -> None:
    parsed = remote_services._parse_expiry("2030-01-01T00:00:00")
    assert parsed is not None and parsed.tzinfo is UTC


def test_an_already_expired_token_still_waits_before_retrying() -> None:
    """A clamp, so a permanently-stale expiry can't spin the refresh loop."""
    past = datetime.now(UTC) - timedelta(days=1)
    token = remote_services.GithubToken(token="t", expires_at=past)
    assert token.seconds_until_refresh() == remote_services.GH_REFRESH_MIN_SECONDS


# ── handing the token to gh ────────────────────────────────────────────────────


def test_hosts_file_is_written_readably_by_gh_and_only_by_us(workbench_home: Path) -> None:
    remote_services._write_gh_hosts("ghs_abc", "octocat")
    path = remote_services.GH_HOSTS_PATH
    assert path.read_text() == (
        'github.com:\n    oauth_token: "ghs_abc"\n    user: "octocat"\n    git_protocol: "https"\n'
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_hosts_file_quoting_survives_an_awkward_login(workbench_home: Path) -> None:
    remote_services._write_gh_hosts('gh"s', "a: b")
    body = remote_services.GH_HOSTS_PATH.read_text()
    assert '"gh\\"s"' in body
    assert '"a: b"' in body


USER_JSON = b'{"login":"octocat","id":583231,"name":"The Octocat","email":null}'


def fake_subprocess(monkeypatch: pytest.MonkeyPatch, user: bytes = USER_JSON, rc: int = 0) -> list[list[str]]:
    """Stub every command _apply_gh_auth shells out to, recording the argv of each."""
    ran: list[list[str]] = []
    real = subprocess.run

    def _fake(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        ran.append(args)
        if args[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(args, rc, user, b"")
        if args[:2] == ["git", "config"]:
            return real(args, **kwargs)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _fake)
    return ran


def test_apply_writes_the_token_and_marks_it_ours(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran = fake_subprocess(monkeypatch)
    assert remote_services._apply_gh_auth(remote_services.GithubToken(token="ghs_abc")) is True
    assert 'oauth_token: "ghs_abc"' in remote_services.GH_HOSTS_PATH.read_text()
    assert remote_services.GH_MANAGED_MARKER.read_text() == "octocat"
    # Never `gh auth login --with-token`: it rejects this token for want of `read:org`.
    assert ["gh", "auth", "setup-git"] in ran
    assert not any("login" in a for a in ran)


def test_a_token_the_api_rejects_is_not_written(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_subprocess(monkeypatch, rc=1)
    assert remote_services._apply_gh_auth(remote_services.GithubToken(token="bad")) is False
    assert not remote_services.GH_HOSTS_PATH.exists()
    assert not remote_services.GH_MANAGED_MARKER.exists()


def test_unparseable_user_json_is_not_written(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_subprocess(monkeypatch, user=b"<html>gateway error</html>")
    assert remote_services._apply_gh_auth(remote_services.GithubToken(token="t")) is False
    assert not remote_services.GH_HOSTS_PATH.exists()


# ── git identity ───────────────────────────────────────────────────────────────


def test_identity_falls_back_to_the_noreply_address_for_a_private_email() -> None:
    """Most accounts keep the email private, so this is the common case, not the edge one."""
    name, email = remote_services.git_identity({"login": "octocat", "id": 583231, "name": "The Octocat"})
    assert name == "The Octocat"
    assert email == "583231+octocat@users.noreply.github.com"


def test_identity_prefers_a_public_email() -> None:
    _, email = remote_services.git_identity({"login": "octocat", "id": 1, "email": "octo@example.com"})
    assert email == "octo@example.com"


def test_identity_falls_back_to_the_login_when_no_name_is_set() -> None:
    name, _ = remote_services.git_identity({"login": "octocat", "id": 1, "name": None})
    assert name == "octocat"


def test_identity_is_seeded_so_committing_works(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_subprocess(monkeypatch)
    remote_services._apply_gh_auth(remote_services.GithubToken(token="ghs_abc"))
    config = remote_services.GIT_CONFIG_PATH.read_text()
    assert "The Octocat" in config
    assert "583231+octocat@users.noreply.github.com" in config


def test_an_identity_the_user_set_is_left_alone(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote_services.GIT_CONFIG_PATH.write_text("[user]\n\tname = Real Name\n\temail = real@example.com\n")
    fake_subprocess(monkeypatch)
    remote_services._apply_gh_auth(remote_services.GithubToken(token="ghs_abc"))
    config = remote_services.GIT_CONFIG_PATH.read_text()
    assert "Real Name" in config
    assert "The Octocat" not in config


def test_a_half_configured_identity_gets_only_the_missing_half(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_services.GIT_CONFIG_PATH.write_text("[user]\n\tname = Real Name\n")
    fake_subprocess(monkeypatch)
    remote_services._apply_gh_auth(remote_services.GithubToken(token="ghs_abc"))
    config = remote_services.GIT_CONFIG_PATH.read_text()
    assert "Real Name" in config
    assert "583231+octocat@users.noreply.github.com" in config


def test_seeding_the_identity_never_touches_the_real_gitconfig(workbench_home: Path) -> None:
    """`git config --global` would otherwise write into the real $HOME."""
    assert remote_services._git_env()["GIT_CONFIG_GLOBAL"] == str(remote_services.GIT_CONFIG_PATH)
    assert remote_services.GIT_CONFIG_PATH.is_relative_to(workbench_home)


# ── not clobbering a login the user made themselves ────────────────────────────


def test_a_fresh_workbench_is_ours_to_authenticate(workbench_home: Path) -> None:
    assert remote_services._gh_is_ours() is True


def test_our_own_login_is_ours_to_refresh(workbench_home: Path) -> None:
    remote_services._write_gh_hosts("ghs_abc", "octocat")
    remote_services.GH_MANAGED_MARKER.parent.mkdir(parents=True, exist_ok=True)
    remote_services.GH_MANAGED_MARKER.write_text("octocat")
    assert remote_services._gh_is_ours() is True


def test_a_login_the_user_made_is_left_alone(workbench_home: Path) -> None:
    """Their token is likely broader-scoped and longer-lived; replacing it is a downgrade."""
    remote_services._write_gh_hosts("gho_user", "octocat")
    assert remote_services._gh_is_ours() is False


def test_seed_does_not_touch_a_user_login(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote_services._write_gh_hosts("gho_user", "octocat")
    before = remote_services.GH_HOSTS_PATH.read_text()
    fake_oauth(monkeypatch, {"accounts": (200, {"accounts": ["o"]}), "token": (200, {"access_token": "ghs_new"})})

    assert run(remote_services.seed_gh_auth()) == remote_services.GH_REFRESH_MAX_SECONDS
    assert remote_services.GH_HOSTS_PATH.read_text() == before


def test_seed_survives_the_oauth_app_being_absent(workbench_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort by design: a workbench with no grant must still start."""
    fake_oauth(monkeypatch, {"accounts": (0, {}), "token": (0, {})})
    assert run(remote_services.seed_gh_auth()) == remote_services.GH_REFRESH_MAX_SECONDS
    assert not remote_services.GH_HOSTS_PATH.exists()


# ── surfacing what the user has to approve ─────────────────────────────────────


def test_grant_url_gets_a_usable_return_to() -> None:
    body = {"required_grant": {"grant_url": "https://oauth.example.com/grant?provider=github&return_to="}}
    assert remote_services.github_action_url(body) == ("https://oauth.example.com/grant?provider=github&return_to=%2F")


def test_an_existing_return_to_is_kept() -> None:
    body = {"required_grant": {"grant_url": "https://oauth.example.com/grant?return_to=%2Fdash"}}
    assert remote_services.github_action_url(body) == "https://oauth.example.com/grant?return_to=%2Fdash"


def test_authorize_url_is_surfaced_for_an_unconnected_account() -> None:
    body = {"status": "authorization_required", "authorize_url": "https://github.com/login/oauth/authorize?x=1"}
    assert remote_services.github_action_url(body) == "https://github.com/login/oauth/authorize?x=1"


def test_no_action_url_when_there_is_nothing_to_approve() -> None:
    assert remote_services.github_action_url({}) == ""
