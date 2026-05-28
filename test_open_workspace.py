"""Tests for the open-workspace endpoint and its repo-access probe.

Run with: .venv/bin/python -m pytest test_open_workspace.py -q

The tests don't touch the network: `_resolve_access` is exercised by stubbing
the `git ls-remote` runner and the token fetch, and the route is exercised by
stubbing `_resolve_access` itself.
"""

from __future__ import annotations

import asyncio

import pytest

import server


def run(coro):
    return asyncio.run(coro)


# ── pure helpers ───────────────────────────────────────────────────────────


def test_repo_dir_name():
    assert server._repo_dir_name("https://github.com/octocat/Hello-World.git") == "Hello-World"
    assert server._repo_dir_name("git@github.com:octocat/Hello-World.git") == "Hello-World"
    assert server._repo_dir_name("https://example.com/a/b/") == "b"


@pytest.mark.parametrize(
    "url, host",
    [
        ("https://github.com/o/r.git", "github.com"),
        ("git@github.com:o/r.git", "github.com"),
        ("ssh://git@github.com/o/r.git", "github.com"),
        ("https://gitlab.com/o/r.git", "gitlab.com"),
    ],
)
def test_git_host(url, host):
    assert server._git_host(url) == host


@pytest.mark.parametrize(
    "url, github",
    [
        ("https://github.com/o/r.git", True),
        ("git@github.com:o/r.git", True),
        ("https://gitlab.com/o/r.git", False),
        ("ssh://git@example.com/o/r.git", False),
    ],
)
def test_is_github(url, github):
    assert server._is_github(url) is github


def test_inject_github_token_https():
    assert (
        server._inject_github_token("https://github.com/o/r.git", "ghs_abc")
        == "https://ghs_abc@github.com/o/r.git"
    )


def test_inject_github_token_leaves_ssh_unchanged():
    # The token can't be applied to an ssh transport.
    assert server._inject_github_token("git@github.com:o/r.git", "ghs_abc") == "git@github.com:o/r.git"
    assert server._inject_github_token("ssh://git@github.com/o/r.git", "t") == "ssh://git@github.com/o/r.git"


# ── _resolve_access classification ───────────────────────────────────────────


def _stub_ls_remote(monkeypatch, results):
    """Feed a queue of (rc, stdout, stderr) tuples to successive ls-remote calls."""
    calls = []
    queue = list(results)

    async def fake(repo, ref, token):
        calls.append((repo, ref, token))
        return queue.pop(0)

    monkeypatch.setattr(server, "_run_ls_remote", fake)
    return calls


def test_resolve_public_repo_ok(monkeypatch):
    _stub_ls_remote(monkeypatch, [(0, "abc\trefs/heads/main\n", "")])
    monkeypatch.setattr(server, "_fetch_github_token", _none_token)
    access = run(server._resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "ok"
    assert access.token == ""


def test_resolve_named_ref_missing_is_404(monkeypatch):
    # Repo reachable (rc 0) but ls-remote returned no matching ref.
    _stub_ls_remote(monkeypatch, [(0, "", "")])
    access = run(server._resolve_access("https://github.com/o/r.git", "nope-branch"))
    assert access.decision == "not_found"


def test_resolve_sha_ref_skips_ref_probe(monkeypatch):
    calls = _stub_ls_remote(monkeypatch, [(0, "", "")])
    access = run(server._resolve_access("https://github.com/o/r.git", "a1b2c3d4"))
    # A sha can't be confirmed via ls-remote, so an empty result is still "ok"
    # and we pass no ref to the probe.
    assert access.decision == "ok"
    assert calls[0][1] is None


def test_resolve_private_github_with_token(monkeypatch):
    # Unauthenticated probe fails, authenticated probe succeeds.
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository not found"), (0, "abc\tHEAD\n", "")])

    async def token():
        return "ghs_tok"

    monkeypatch.setattr(server, "_fetch_github_token", token)
    access = run(server._resolve_access("https://github.com/o/private.git", "main"))
    assert access.decision == "ok"
    assert access.token == "ghs_tok"


def test_resolve_github_not_found_even_with_token(monkeypatch):
    _stub_ls_remote(monkeypatch, [(128, "", "not found"), (128, "", "not found")])

    async def token():
        return "ghs_tok"

    monkeypatch.setattr(server, "_fetch_github_token", token)
    access = run(server._resolve_access("https://github.com/o/ghost.git", "main"))
    assert access.decision == "not_found"


def test_resolve_private_github_no_token_is_forbidden(monkeypatch):
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository not found")])
    monkeypatch.setattr(server, "_fetch_github_token", _none_token)
    access = run(server._resolve_access("https://github.com/o/private.git", "main"))
    assert access.decision == "forbidden"


def test_resolve_non_github_auth_error_is_forbidden(monkeypatch):
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: Authentication failed for 'https://gitlab.com/o/r.git'")])
    access = run(server._resolve_access("https://gitlab.com/o/r.git", "main"))
    assert access.decision == "forbidden"


def test_resolve_non_github_not_found(monkeypatch):
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: repository 'https://gitlab.com/o/ghost.git/' not found")])
    access = run(server._resolve_access("https://gitlab.com/o/ghost.git", "main"))
    assert access.decision == "not_found"


def test_resolve_network_error(monkeypatch):
    _stub_ls_remote(monkeypatch, [(128, "", "fatal: unable to access: Could not resolve host: github.com")])
    access = run(server._resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "error"


def test_resolve_timeout_is_error(monkeypatch):
    _stub_ls_remote(monkeypatch, [(124, "", "timed out")])
    access = run(server._resolve_access("https://github.com/o/r.git", "main"))
    assert access.decision == "error"


async def _none_token():
    return ""


# ── route behavior ───────────────────────────────────────────────────────────


def _stub_access(monkeypatch, decision, token=""):
    async def fake(repo, ref):
        return server.RepoAccess(decision, token=token)

    monkeypatch.setattr(server, "_resolve_access", fake)


def _post(form=None, json=None, query=None):
    # Quart's test client treats json/form/data as mutually exclusive, so only
    # pass whichever the test actually set.
    kwargs: dict = {}
    if form is not None:
        kwargs["form"] = form
    if json is not None:
        kwargs["json"] = json
    if query is not None:
        kwargs["query_string"] = query

    async def go():
        client = server.app.test_client()
        return await client.post("/open-workspace", **kwargs)

    return run(go())


def test_missing_repo_is_400(monkeypatch):
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"ref": "main"})
    assert resp.status_code == 400


def test_missing_ref_is_400(monkeypatch):
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": "https://github.com/o/r.git"})
    assert resp.status_code == 400


@pytest.mark.parametrize("repo", ["file:///etc/passwd", "ext::sh -c id", "not-a-url", "/tmp/x"])
def test_bad_transport_is_400(monkeypatch, repo):
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": repo, "ref": "main"})
    assert resp.status_code == 400


@pytest.mark.parametrize("ref", ["-rf", "--upload-pack=x", "a b", "a;b", "a$b", "../etc"])
def test_unsafe_ref_is_400(monkeypatch, ref):
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": ref})
    assert resp.status_code == 400


def test_forbidden_is_403(monkeypatch):
    _stub_access(monkeypatch, "forbidden")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 403
    assert "Location" not in resp.headers


def test_not_found_is_404(monkeypatch):
    _stub_access(monkeypatch, "not_found")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 404


def test_internal_error_is_500(monkeypatch):
    _stub_access(monkeypatch, "error")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 500


def test_success_redirects_303_with_location(monkeypatch):
    server._pending.clear()
    _stub_access(monkeypatch, "ok")
    resp = _post(form={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 303
    assert resp.headers["Location"].startswith("/?session=")
    # A pending session was queued for the PTY to pick up.
    assert len(server._pending) == 1
    sid = next(iter(server._pending))
    pending = server._pending[sid]
    assert pending.env["WORKSPACE_REPO"] == "https://github.com/o/r.git"
    assert pending.env["WORKSPACE_REF"] == "main"
    assert pending.env["WORKSPACE_DIR"] == "r"
    assert "WORKSPACE_GITHUB_TOKEN" not in pending.env


def test_success_passes_token_to_pending(monkeypatch):
    server._pending.clear()
    _stub_access(monkeypatch, "ok", token="ghs_secret")
    resp = _post(form={"repo": "https://github.com/o/private.git", "ref": "abc1234"})
    assert resp.status_code == 303
    sid = next(iter(server._pending))
    assert server._pending[sid].env["WORKSPACE_GITHUB_TOKEN"] == "ghs_secret"


def test_accepts_json_body(monkeypatch):
    _stub_access(monkeypatch, "ok")
    resp = _post(json={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 303


def test_accepts_query_params(monkeypatch):
    _stub_access(monkeypatch, "ok")
    resp = _post(query={"repo": "https://github.com/o/r.git", "ref": "main"})
    assert resp.status_code == 303


def test_debug_route_is_gone():
    async def go():
        client = server.app.test_client()
        return await client.get("/debug", query_string={"repo": "https://github.com/o/r.git"})

    resp = run(go())
    assert resp.status_code == 404
