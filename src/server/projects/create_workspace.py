"""Brings up one workspace: mirror, clone, check out, branch, run the project's setup command.

Run as a child of the tab's login shell, which launches Claude afterwards -- see
`server.projects.launch`. Every input arrives in the environment rather than on the command line,
so nothing here is interpolated into a shell.

    WS_PATH          absolute path of the workspace dir (already created, empty)
    WS_REPO          clone URL, kept token-free: this is what origin ends up pointing at
    WS_MIRROR        absolute path of the project's bare mirror
    WS_REF           branch/tag/sha to check out. The server normally fills this in with the
                     project's configured default branch, or the branch the remote's HEAD points
                     at right now; blank falls back to whatever the mirror's HEAD says.
    WS_BRANCH        optional new branch to create and switch to after the checkout, so an
                     automated run starts on the branch its PR will come from
    WS_SETUP         optional one-off setup command, run in the workspace
    WS_GITHUB_TOKEN  optional transient token, used for network git only and never written to disk

The exit status is the whole interface to the shell that runs this: zero means "the workspace is
usable, start Claude in it", non-zero means "leave the user in a shell here instead". Only the two
clone failures are non-zero, because a half-made workspace you can inspect beats a tab that
vanished -- a failed checkout or a failed setup command still leaves something worth working in.
"""

import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

# Neither of these may ever block on a prompt: there is no one at this terminal yet, and a git that
# stops to ask for a password would hang the tab instead of failing it.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}

FATAL = 1


def log(message: str) -> None:
    print(f"[workbench] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[workbench] {message}", file=sys.stderr, flush=True)


def authed_url(repo_url: str, token: str) -> str:
    """`repo_url` with `token` embedded, for the network calls only.

    The token goes into the URL used to reach the remote and never into the URL persisted as
    origin, so it can't be read back out of a workspace's or a mirror's git config afterwards. Only
    http(s) can carry one; anything else is returned untouched.
    """
    if not token:
        return repo_url
    encoded = urllib.parse.quote(token, safe="")
    for scheme in ("https://", "http://"):
        if repo_url.startswith(scheme):
            return f"{scheme}{encoded}@{repo_url[len(scheme) :]}"
    return repo_url


def git(*args: str, cwd: Path | None = None) -> bool:
    """Run a git command, echoing it, and report whether it succeeded.

    Output is inherited rather than captured: the whole point of doing this in the tab is that the
    person who asked for the workspace watches it happen.
    """
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, check=False).returncode == 0


def update_mirror(mirror: Path, repo_url: str, token: str) -> bool:
    """Bring the project's bare mirror up to date, creating it if this is the first workspace.

    The mirror is what makes a second workspace cheap, and this fetch is the only thing standing
    between a new workspace and a checkout that is behind upstream -- so every branch and every
    tag, every time.
    """
    url = authed_url(repo_url, token)
    if mirror.is_dir():
        log(f"updating mirror for {repo_url}")
        git("--git-dir", str(mirror), "remote", "set-url", "origin", url)
        if not git(
            "--git-dir",
            str(mirror),
            "fetch",
            "--prune",
            "origin",
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
        ):
            warn("mirror fetch failed; falling back to the copy on disk, which may be behind upstream.")
        # Unconditional, including after a failed fetch: leave no token behind in the config.
        git("--git-dir", str(mirror), "remote", "set-url", "origin", repo_url)
        return True

    log(f"mirroring {repo_url} (first workspace for this project)")
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if not git("clone", "--mirror", "--", url, str(mirror)):
        warn("mirror clone failed; dropping you into a shell.")
        shutil.rmtree(mirror, ignore_errors=True)
        return False
    git("--git-dir", str(mirror), "remote", "set-url", "origin", repo_url)
    return True


def clone_workspace(mirror: Path, path: Path, repo_url: str) -> bool:
    """Clone the workspace out of the local mirror: hardlinked objects, so this is fast and nearly
    free on disk however many workspaces a project has."""
    log(f"creating workspace at {path}")
    if not git("clone", "--", str(mirror), str(path)):
        warn("workspace clone failed; dropping you into a shell.")
        return False
    # origin points at the mirror after that clone; repoint it at the real remote so push/pull and
    # anything reading the remote URL behave the way they would in an ordinary checkout.
    git("remote", "set-url", "origin", repo_url, cwd=path)
    return True


def checkout(path: Path, ref: str) -> None:
    log(f"checking out {ref}")
    if not git("checkout", ref, cwd=path):
        warn(f"checkout of {ref} failed; staying on the default branch.")


def switch_to_branch(path: Path, branch: str) -> None:
    """Put a run that is going to open a PR on its own branch, so nothing it does can land on the
    branch it was cloned from. Falls back to joining the branch when it already exists, so this is
    safe to repeat over a workspace a previous attempt got part way through."""
    log(f"switching to branch {branch}")
    if git("checkout", "-b", branch, cwd=path):
        return
    if not git("checkout", branch, cwd=path):
        warn(f"could not switch to {branch}; staying on the current branch.")


def run_setup(path: Path, setup: str) -> None:
    print(flush=True)
    log(f"running project setup: {setup}")
    # A login shell because the setup command is written by a person, for this container, and is
    # entitled to the same PATH and profile any terminal here would give it.
    if subprocess.run(["bash", "-lc", setup], cwd=path, check=False).returncode != 0:
        warn("setup command failed; continuing anyway.")


def main() -> int:
    path = Path(os.environ["WS_PATH"])
    repo_url = os.environ["WS_REPO"]
    mirror = Path(os.environ["WS_MIRROR"])
    token = os.environ.get("WS_GITHUB_TOKEN", "")

    print(flush=True)
    if not update_mirror(mirror, repo_url, token):
        return FATAL
    if not clone_workspace(mirror, path, repo_url):
        return FATAL

    ref = os.environ.get("WS_REF", "")
    if ref:
        checkout(path, ref)

    branch = os.environ.get("WS_BRANCH", "")
    if branch:
        switch_to_branch(path, branch)

    setup = os.environ.get("WS_SETUP", "")
    if setup:
        run_setup(path, setup)

    print(flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
