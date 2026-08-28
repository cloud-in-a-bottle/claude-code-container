from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import Coroutine
from typing import Any

import pytest

from server import git_remote


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


# ── pure helpers ───────────────────────────────────────────────────────────────


def test_repo_dir_name() -> None:
    assert git_remote.repo_dir_name("https://github.com/octocat/Hello-World.git") == "Hello-World"
    assert git_remote.repo_dir_name("git@github.com:octocat/Hello-World.git") == "Hello-World"
    assert git_remote.repo_dir_name("https://example.com/a/b/") == "b"


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
    assert git_remote.git_host(url) == host


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
    assert git_remote.is_github(url) is github


def test_inject_github_token_https() -> None:
    assert (
        git_remote.inject_github_token("https://github.com/o/r.git", "ghs_abc") == "https://ghs_abc@github.com/o/r.git"
    )


def test_inject_github_token_leaves_ssh_unchanged() -> None:
    assert git_remote.inject_github_token("git@github.com:o/r.git", "ghs_abc") == "git@github.com:o/r.git"
    assert git_remote.inject_github_token("ssh://git@github.com/o/r.git", "t") == "ssh://git@github.com/o/r.git"


# ── the remote's own default branch ───────────────────────────────────────────


def test_resolve_default_branch_reads_the_symref(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_ls_remote(
        monkeypatch,
        [(0, "ref: refs/heads/trunk\tHEAD\n1111111111111111111111111111111111111111\tHEAD\n", "")],
    )
    assert run(git_remote.resolve_default_branch("https://github.com/o/r.git", token="ghs_tok")) == "trunk"
    assert calls == [("https://github.com/o/r.git", "HEAD", "ghs_tok")]


def test_resolve_default_branch_handles_a_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(0, "ref: refs/heads/release/2.x\tHEAD\n", "")])
    assert run(git_remote.resolve_default_branch("https://github.com/o/r.git")) == "release/2.x"


@pytest.mark.parametrize(
    "result",
    [
        (128, "", "fatal: could not read Username"),  # unreachable
        (0, "1111111111111111111111111111111111111111\tHEAD\n", ""),  # no symref line (old git)
        (0, "", ""),  # empty repo
    ],
)
def test_resolve_default_branch_gives_up_quietly(
    monkeypatch: pytest.MonkeyPatch, result: tuple[int, str, str]
) -> None:
    """Callers fall back to the mirror's HEAD, so an unreadable answer must not raise."""
    _stub_ls_remote(monkeypatch, [result])
    assert run(git_remote.resolve_default_branch("https://github.com/o/r.git")) == ""


# ── _resolve_access classification ────────────────────────────────────────────


def _stub_ls_remote(
    monkeypatch: pytest.MonkeyPatch, results: list[tuple[int, str, str]]
) -> list[tuple[str, str | None, str]]:
    """Feed a queue of (rc, stdout, stderr) tuples to successive ls-remote calls."""
    calls: list[tuple[str, str | None, str]] = []
    queue = list(results)

    async def fake(repo: str, ref: str | None, token: str, symref: bool = False) -> tuple[int, str, str]:
        calls.append((repo, ref, token))
        return queue.pop(0)

    monkeypatch.setattr(git_remote, "run_ls_remote", fake)
    return calls


async def _none_token() -> str:
    return ""


def test_resolve_public_repo_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(0, "abc\trefs/heads/main\n", "")])
    monkeypatch.setattr(git_remote, "fetch_github_token", _none_token)
    access = run(git_remote.resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "ok"
    assert access.token == ""


def test_resolve_named_ref_missing_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(0, "", "")])
    access = run(git_remote.resolve_access("https://github.com/o/r.git", "nope-branch"))
    assert access.decision == "not_found"


def test_resolve_sha_ref_skips_ref_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_ls_remote(monkeypatch, [(0, "", "")])
    access = run(git_remote.resolve_access("https://github.com/o/r.git", "a1b2c3d4"))
    assert access.decision == "ok"
    assert calls[0][1] is None


def test_resolve_private_github_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository not found"), (0, "abc\tHEAD\n", "")])

    async def token() -> str:
        return "ghs_tok"

    monkeypatch.setattr(git_remote, "fetch_github_token", token)
    access = run(git_remote.resolve_access("https://github.com/o/private.git", "main"))
    assert access.decision == "ok"
    assert access.token == "ghs_tok"


def test_resolve_github_not_found_even_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "not found"), (128, "", "not found")])

    async def token() -> str:
        return "ghs_tok"

    monkeypatch.setattr(git_remote, "fetch_github_token", token)
    access = run(git_remote.resolve_access("https://github.com/o/ghost.git", "main"))
    assert access.decision == "not_found"


def test_resolve_private_github_no_token_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository not found")])
    monkeypatch.setattr(git_remote, "fetch_github_token", _none_token)
    access = run(git_remote.resolve_access("https://github.com/o/private.git", "main"))
    assert access.decision == "forbidden"


def test_resolve_non_github_auth_error_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: Authentication failed for 'https://gitlab.com/o/r.git'")])
    access = run(git_remote.resolve_access("https://gitlab.com/o/r.git", "main"))
    assert access.decision == "forbidden"


def test_resolve_non_github_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository 'https://gitlab.com/o/ghost.git/' not found")])
    access = run(git_remote.resolve_access("https://gitlab.com/o/ghost.git", "main"))
    assert access.decision == "not_found"


def test_resolve_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: unable to access: Could not resolve host: github.com")])
    access = run(git_remote.resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "error"


def test_resolve_timeout_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_ls_remote(monkeypatch, [(124, "", "timed out")])
    access = run(git_remote.resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "error"


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
    assert git_remote.inject_github_token("https://github.com/o/r.git", token) == expected


@pytest.mark.parametrize(
    "token",
    ["evil@attacker.com", "foo:bar", "a/b", "pct%20", "a b", "a\nb"],
)
def test_inject_github_token_preserves_host_under_unsafe_input(token: str) -> None:
    """No token shape may shift the URL's host away from github.com."""
    out = git_remote.inject_github_token("https://github.com/o/r.git", token)
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
    assert git_remote.inject_github_token("https://github.com:8443/o/r.git", token) == expected


def test_inject_github_token_empty_token_still_safe() -> None:
    # An empty token shouldn't reach `inject_github_token` (the caller checks
    # first), but if it ever did, the URL must remain syntactically valid.
    out = git_remote.inject_github_token("https://github.com/o/r.git", "")
    parsed = urllib.parse.urlparse(out)
    assert parsed.hostname == "github.com"
