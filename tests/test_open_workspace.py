from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import Coroutine
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest

from server import app as srv
from server import tab_store
from server import tabs
from server import workspace


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clear_tabs() -> Generator[None]:
    tabs._tabs.clear()
    yield
    tabs._tabs.clear()


# ── pure helpers ───────────────────────────────────────────────────────────────


def test_repo_dir_name() -> None:
    assert workspace.repo_dir_name("https://github.com/octocat/Hello-World.git") == "Hello-World"
    assert workspace.repo_dir_name("git@github.com:octocat/Hello-World.git") == "Hello-World"
    assert workspace.repo_dir_name("https://example.com/a/b/") == "b"


@pytest.mark.parametrize(
    "url, host",
    [
        ("https://github.com/o/r.git", "github.com"),
        ("git@github.com:o/r.git", "github.com"),
        ("ssh://git@github.com/o/r.git", "github.com"),
        ("https://gitlab.com/o/r.git", "gitlab.com"),
    ],
)
def test_git_host(url: str, host: str) -> None:
    assert workspace.git_host(url) == host


@pytest.mark.parametrize(
    "url, github",
    [
        ("https://github.com/o/r.git", True),
        ("git@github.com:o/r.git", True),
        ("https://gitlab.com/o/r.git", False),
        ("ssh://git@example.com/o/r.git", False),
    ],
)
def test_is_github(url: str, github: bool) -> None:
    assert workspace.is_github(url) is github


def test_inject_github_token_https() -> None:
    assert (
        workspace.inject_github_token("https://github.com/o/r.git", "ghs_abc") == "https://ghs_abc@github.com/o/r.git"
    )


def test_inject_github_token_leaves_ssh_unchanged() -> None:
    assert workspace.inject_github_token("git@github.com:o/r.git", "ghs_abc") == "git@github.com:o/r.git"
    assert workspace.inject_github_token("ssh://git@github.com/o/r.git", "t") == "ssh://git@github.com/o/r.git"


# ── _resolve_access classification ────────────────────────────────────────────


def _stub_ls_remote(
    monkeypatch: pytest.MonkeyPatch, results: list[tuple[int, str, str]]
) -> list[tuple[str, str | None, str]]:
    """Feed a queue of (rc, stdout, stderr) tuples to successive ls-remote calls."""
    calls: list[tuple[str, str | None, str]] = []
    queue = list(results)

    async def fake(repo: str, ref: str | None, token: str) -> tuple[int, str, str]:
        calls.append((repo, ref, token))
        return queue.pop(0)

    monkeypatch.setattr(workspace, "run_ls_remote", fake)
    return calls


async def _none_token() -> str:
    return ""


def test_resolve_public_repo_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(0, "abc\trefs/heads/main\n", "")])
    monkeypatch.setattr(workspace, "fetch_github_token", _none_token)
    access = run(workspace.resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "ok"
    assert access.token == ""


def test_resolve_named_ref_missing_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(0, "", "")])
    access = run(workspace.resolve_access("https://github.com/o/r.git", "nope-branch"))
    assert access.decision == "not_found"


def test_resolve_sha_ref_skips_ref_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_ls_remote(monkeypatch, [(0, "", "")])
    access = run(workspace.resolve_access("https://github.com/o/r.git", "a1b2c3d4"))
    assert access.decision == "ok"
    assert calls[0][1] is None


def test_resolve_private_github_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository not found"), (0, "abc\tHEAD\n", "")])

    async def token() -> str:
        return "ghs_tok"

    monkeypatch.setattr(workspace, "fetch_github_token", token)
    access = run(workspace.resolve_access("https://github.com/o/private.git", "main"))
    assert access.decision == "ok"
    assert access.token == "ghs_tok"


def test_resolve_github_not_found_even_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "not found"), (128, "", "not found")])

    async def token() -> str:
        return "ghs_tok"

    monkeypatch.setattr(workspace, "fetch_github_token", token)
    access = run(workspace.resolve_access("https://github.com/o/ghost.git", "main"))
    assert access.decision == "not_found"


def test_resolve_private_github_no_token_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository not found")])
    monkeypatch.setattr(workspace, "fetch_github_token", _none_token)
    access = run(workspace.resolve_access("https://github.com/o/private.git", "main"))
    assert access.decision == "forbidden"


def test_resolve_non_github_auth_error_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: Authentication failed for 'https://gitlab.com/o/r.git'")])
    access = run(workspace.resolve_access("https://gitlab.com/o/r.git", "main"))
    assert access.decision == "forbidden"


def test_resolve_non_github_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository 'https://gitlab.com/o/ghost.git/' not found")])
    access = run(workspace.resolve_access("https://gitlab.com/o/ghost.git", "main"))
    assert access.decision == "not_found"


def test_resolve_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: unable to access: Could not resolve host: github.com")])
    access = run(workspace.resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "error"


def test_resolve_timeout_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(124, "", "timed out")])
    access = run(workspace.resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "error"


# ── route behavior ─────────────────────────────────────────────────────────────


def _stub_access(monkeypatch: pytest.MonkeyPatch, decision: str, token: str = "") -> None:
    async def fake(repo: str, ref: str) -> workspace.RepoAccess:
        return workspace.RepoAccess(decision=decision, token=token)

    monkeypatch.setattr(srv, "resolve_access", fake)


def _stub_create_tab(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub create_server_tab to avoid spawning real processes; returns a log of calls."""
    created: list[dict[str, Any]] = []

    async def fake(
        *,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin_seed: str = "",
        label: str | None = None,
        kind: str = tab_store.SHELL,
        tab_id: str | None = None,
    ) -> tabs.ServerTab:
        tab = tabs.ServerTab(id=tab_id or "test-id", label=label or "test", master_fd=-1, proc=MagicMock())
        tabs._tabs[tab.id] = tab
        created.append({"command": command, "cwd": cwd, "env": env or {}, "label": label, "kind": kind})
        return tab

    monkeypatch.setattr(srv, "create_server_tab", fake)
    return created


def _post(
    form: dict[str, str] | None = None,
    json: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> Any:
    kwargs: dict[str, Any] = {}
    if form is not None:
        kwargs["form"] = form
    if json is not None:
        kwargs["json"] = json
    if query is not None:
        kwargs["query_string"] = query

    async def go() -> Any:
        client = srv.app.test_client()
        return await client.post("/open-workspace", **kwargs)

    return run(go())


def test_missing_repo_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"ref": "main"})
    assert resp.status_code == 400


def test_missing_ref_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": "https://github.com/o/r.git"})
    assert resp.status_code == 400


@pytest.mark.parametrize("repo", ["file:///etc/passwd", "ext::sh -c id", "not-a-url", "/tmp/x"])
def test_bad_transport_is_400(monkeypatch: pytest.MonkeyPatch, repo: str) -> None:
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": repo, "ref": "main"})
    assert resp.status_code == 400


@pytest.mark.parametrize("ref", ["-rf", "--upload-pack=x", "a b", "a;b", "a$b", "../etc"])
def test_unsafe_ref_is_400(monkeypatch: pytest.MonkeyPatch, ref: str) -> None:
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": ref})
    assert resp.status_code == 400


def test_forbidden_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "forbidden")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 403
    assert "Location" not in resp.headers


def test_not_found_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "not_found")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 404


def test_internal_error_is_500(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "error")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 500


def test_success_redirects_303_with_location(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    created = _stub_create_tab(monkeypatch)
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 303
    assert resp.headers["Location"].startswith("/?tab=")
    assert len(created) == 1
    env = created[0]["env"]
    assert env["WORKSPACE_REPO"] == "https://github.com/o/r.git"
    assert env["WORKSPACE_REF"] == "main"
    assert env["WORKSPACE_DIR"] == "r"
    assert "WORKSPACE_GITHUB_TOKEN" not in env


def test_workspace_tab_restores_as_a_shell_not_a_re_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The script clones and then execs a shell. Restoring must not run the clone again."""
    _stub_access(monkeypatch, "ok")
    created = _stub_create_tab(monkeypatch)
    _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert created[0]["kind"] == tab_store.SHELL


def test_success_passes_token_to_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok", token="ghs_secret")
    created = _stub_create_tab(monkeypatch)
    resp = _post(form={"repo": "https://github.com/o/private.git", "ref": "abc1234"})
    assert resp.status_code == 303
    assert created[0]["env"]["WORKSPACE_GITHUB_TOKEN"] == "ghs_secret"


# ── token URL injection: malformed/invalid token shapes ───────────────────────
#
# GitHub tokens today are `[A-Za-z0-9_]`, but the oauth provider is external —
# we can't statically guarantee what it returns. These tests pin that no
# adversarial or accidentally-malformed token can corrupt the URL or splice
# extra hosts/credentials into the authority section.


@pytest.mark.parametrize(
    "token, expected",
    [
        # The happy path: a real GitHub token shape passes through unchanged
        # (underscore is unreserved per RFC 3986, no encoding needed).
        ("ghs_AbCd1234", "https://ghs_AbCd1234@github.com/o/r.git"),
        # `@` in the token would otherwise let the value re-anchor the URL's
        # authority and point git at an attacker-chosen host.
        ("evil@attacker.com", "https://evil%40attacker.com@github.com/o/r.git"),
        # `:` would otherwise be parsed as a user:password separator.
        ("foo:bar", "https://foo%3Abar@github.com/o/r.git"),
        # `/` would otherwise terminate the authority section and shift the
        # rest of the token into the path, silently changing the repo path.
        ("a/b", "https://a%2Fb@github.com/o/r.git"),
        # `%` is the encoding sigil — must itself be encoded so a literal
        # `%` in the token isn't re-decoded as something else.
        ("pct%20", "https://pct%2520@github.com/o/r.git"),
        # Spaces and other whitespace must be encoded — a raw space in the URL
        # would make git reject the URL outright.
        ("a b", "https://a%20b@github.com/o/r.git"),
        # Newline: a non-encoding implementation could log/leak the URL with a
        # line break embedded in the credential. Belt-and-suspenders.
        ("a\nb", "https://a%0Ab@github.com/o/r.git"),
    ],
)
def test_inject_github_token_encodes_unsafe_chars(token: str, expected: str) -> None:
    assert workspace.inject_github_token("https://github.com/o/r.git", token) == expected


@pytest.mark.parametrize(
    "token",
    ["evil@attacker.com", "foo:bar", "a/b", "pct%20", "a b", "a\nb"],
)
def test_inject_github_token_preserves_host_under_unsafe_input(token: str) -> None:
    """No token shape may shift the URL's host away from github.com."""
    out = workspace.inject_github_token("https://github.com/o/r.git", token)
    parsed = urllib.parse.urlparse(out)
    assert parsed.hostname == "github.com"
    assert parsed.path == "/o/r.git"


@pytest.mark.parametrize(
    "token, expected",
    [
        # Happy path with a port — the port must survive the netloc rewrite.
        ("ghs_AbCd1234", "https://ghs_AbCd1234@github.com:8443/o/r.git"),
        # `@` in the token would otherwise shift the port (and the host) out
        # from under us — verify the same encoding holds here.
        ("evil@attacker.com", "https://evil%40attacker.com@github.com:8443/o/r.git"),
        # `:` in the token would otherwise look like the user:port separator
        # and re-anchor the port.
        ("foo:bar", "https://foo%3Abar@github.com:8443/o/r.git"),
    ],
)
def test_inject_github_token_preserves_port(token: str, expected: str) -> None:
    """Explicit-port URLs must round-trip with the port intact, including under
    unsafe token shapes that target the port/host boundary."""
    assert workspace.inject_github_token("https://github.com:8443/o/r.git", token) == expected


def test_inject_github_token_empty_token_still_safe() -> None:
    # An empty token shouldn't reach `inject_github_token` (the caller checks
    # first), but if it ever did, the URL must remain syntactically valid.
    out = workspace.inject_github_token("https://github.com/o/r.git", "")
    parsed = urllib.parse.urlparse(out)
    assert parsed.hostname == "github.com"


def test_accepts_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    _stub_create_tab(monkeypatch)
    resp = _post(json={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 303


def test_accepts_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_access(monkeypatch, "ok")
    _stub_create_tab(monkeypatch)
    resp = _post(query={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 303


def test_accepts_get_with_query(monkeypatch: pytest.MonkeyPatch) -> None:
    # GET is supported as a workaround for the openhost router's 302→/login
    # bounce, which demotes the eventual POST back to a GET. The same query
    # params that work on POST must also work on GET.
    _stub_access(monkeypatch, "ok")
    _stub_create_tab(monkeypatch)

    async def go() -> Any:
        client = srv.app.test_client()
        return await client.get(
            "/open-workspace",
            query_string={"repo": "https://github.com/o/r.git", "ref": "main"},
        )

    resp = run(go())
    assert resp.status_code == 303
    assert resp.headers["Location"].startswith("/?tab=")


def test_debug_route_is_gone() -> None:
    async def go() -> Any:
        client = srv.app.test_client()
        return await client.get("/debug", query_string={"repo": "https://github.com/o/r.git"})

    resp = run(go())
    assert resp.status_code == 404
