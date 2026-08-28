import asyncio
import os
import re
import subprocess
import urllib.parse

import attr

from server.remote_services import fetch_github_token

# A git ref/sha: no leading dash (would be read as a `git checkout` flag) and a conservative
# character set.
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# The `ref: refs/heads/<branch>\tHEAD` line `git ls-remote --symref` prints for the remote's HEAD.
SYMREF_HEAD_RE = re.compile(r"^ref:\s+refs/heads/(\S+)\s+HEAD$", re.MULTILINE)

# A ref that looks like a bare commit sha. `git ls-remote` only lists named refs, so a sha can't
# be validated ahead of the clone — we skip the pre-check and let the checkout degrade gracefully.
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

_NETWORK_ERR_RE = re.compile(
    r"could ?n.?t resolve host|could not resolve|failed to connect|connection (timed out|refused)"
    r"|could not connect|network is unreachable|temporary failure in name resolution",
    re.IGNORECASE,
)
_AUTH_ERR_RE = re.compile(
    r"authentication failed|could not read username|could not read password"
    r"|terminal prompts disabled|permission denied|access denied|denied to|403 forbidden",
    re.IGNORECASE,
)


@attr.s(auto_attribs=True, frozen=True)
class RepoAccess:
    decision: str  # "ok" | "forbidden" | "not_found" | "error"
    token: str = ""
    detail: str = ""


def repo_dir_name(url: str) -> str:
    """Derive a safe local directory name from a clone URL."""
    name = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    name = re.sub(r"[^A-Za-z0-9._-]", "", name)
    return name or "repo"


def git_host(url: str) -> str:
    scp = re.match(r"^[A-Za-z0-9._-]+@([^:/]+):", url)
    if scp:
        return scp.group(1).lower()
    return (urllib.parse.urlparse(url).hostname or "").lower()


def validate_repo_url(url: str) -> bool:
    if not url or any(c.isspace() for c in url):
        return False
    if not git_host(url):
        return False
    if "://" in url:
        if urllib.parse.urlparse(url).scheme not in ("http", "https", "ssh"):
            return False
    return True


def is_github(url: str) -> bool:
    host = git_host(url)
    return host == "github.com" or host.endswith(".github.com")


def inject_github_token(url: str, token: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
        encoded = urllib.parse.quote(token, safe="")
        return parsed._replace(netloc=f"{encoded}@{host}").geturl()
    return url


async def run_ls_remote(repo: str, ref: str | None, token: str, symref: bool = False) -> tuple[int, str, str]:
    url = inject_github_token(repo, token) if token else repo
    args = ["git", "ls-remote", *(["--symref"] if symref else []), url]
    if ref:
        args.append(ref)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}
    proc = await asyncio.create_subprocess_exec(*args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=25)
    except TimeoutError:
        proc.kill()
        return 124, "", "timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def resolve_access(repo: str, ref: str) -> RepoAccess:
    ref_probe = None if SHA_RE.match(ref) else ref

    rc, out, err = await run_ls_remote(repo, ref_probe, token="")
    if rc == 0:
        if ref_probe is not None and not out.strip():
            return RepoAccess(decision="not_found", detail=f"ref {ref!r} not found")
        return RepoAccess(decision="ok")
    if rc == 124 or _NETWORK_ERR_RE.search(err):
        return RepoAccess(decision="error", detail=err.strip())

    if is_github(repo):
        token = await fetch_github_token()
        if token and inject_github_token(repo, token) != repo:
            rc2, out2, err2 = await run_ls_remote(repo, ref_probe, token=token)
            if rc2 == 0:
                if ref_probe is not None and not out2.strip():
                    return RepoAccess(decision="not_found", detail=f"ref {ref!r} not found")
                return RepoAccess(decision="ok", token=token)
            if rc2 == 124 or _NETWORK_ERR_RE.search(err2):
                return RepoAccess(decision="error", detail=err2.strip())
            return RepoAccess(decision="not_found", detail=err2.strip())
        return RepoAccess(decision="forbidden", detail=err.strip())

    if _AUTH_ERR_RE.search(err):
        return RepoAccess(decision="forbidden", detail=err.strip())
    return RepoAccess(decision="not_found", detail=err.strip())


async def resolve_default_branch(repo: str, token: str = "") -> str:
    """The branch the remote's HEAD points at, e.g. `main`.

    Read from the remote every time rather than cached on the project: a repo that renames its
    default branch should be followed, not remembered wrong. `""` when it can't be read — callers
    fall back to whatever the local mirror's HEAD says, which is the older, staler answer.
    """
    rc, out, _err = await run_ls_remote(repo, "HEAD", token=token, symref=True)
    if rc != 0:
        return ""
    match = SYMREF_HEAD_RE.search(out)
    return match.group(1) if match else ""
