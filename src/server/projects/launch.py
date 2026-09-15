import shlex
import shutil
import sys

from server.billing import workspace_auth_env
from server.claude_launch import claude_session_command
from server.projects.store import Project
from server.projects.workspaces import Workspace
from server.projects.workspaces import mirror_path
from server.tab_store import CLAUDE
from server.tabs import ServerTab
from server.tabs import create_server_tab
from server.tabs import new_session_id

BOOTSTRAP_MODULE = "server.projects.create_workspace"


def bootstrap_command(claude_bin: str, session_id: str, prompt: str = "") -> list[str]:
    """The tab's command: build the workspace, then hand the terminal over to Claude.

    A login shell wraps both halves because that is where this container's PATH additions come
    from (`/etc/profile.d/workbench.sh`), and Claude inherits them — losing them would take
    `~/.local/bin` out from under it.

    The bootstrap's exit status decides whether Claude starts at all: non-zero means the clone
    failed and the user is better off in a shell in the wreckage than watching Claude open a
    conversation about an empty directory. The token is unset either way, before anything the user
    can type at, so it cannot be read out of their shell's environment.
    """
    bootstrap = shlex.join([sys.executable, "-m", BOOTSTRAP_MODULE])
    attempt = claude_session_command(claude_bin, session_id, resume_first=False, prompt=prompt)
    return [
        "bash",
        "-l",
        "-c",
        f'{bootstrap}; status=$?; unset WS_GITHUB_TOKEN; [ "$status" = 0 ] && {{ {attempt}; }}; exec bash',
    ]


async def start_workspace_tab(
    project: Project,
    workspace: Workspace,
    ref: str = "",
    github_token: str = "",
    branch: str = "",
    prompt: str = "",
) -> ServerTab:
    """Open the tab that builds a fresh workspace and then hands over to Claude.

    kind=CLAUDE even though the command starts with a bootstrap: a restore must re-enter the
    conversation in the finished workspace, never run the clone a second time.

    `branch` and `prompt` are what an automated run adds over a hand-made workspace: a branch to
    work on, and an opening message so the conversation starts already doing the work. Both are
    empty for a workspace someone created themselves, which then opens an idle Claude as before.
    """
    session_id = new_session_id()
    env: dict[str, str | None] = {
        "WS_PATH": str(workspace.path),
        "WS_REPO": project.repo_url,
        "WS_MIRROR": str(mirror_path(project.id)),
        "WS_REF": ref,
        "WS_BRANCH": branch,
        "WS_SETUP": project.setup,
    }
    if github_token:
        env["WS_GITHUB_TOKEN"] = github_token
    # The workspace's billing mode is pinned before this runs, so the bootstrap hands Claude the
    # same auth every later tab in the workspace will get.
    env.update(await workspace_auth_env(workspace.id))

    return await create_server_tab(
        command=bootstrap_command(shutil.which("claude") or "claude", session_id, prompt),
        cwd=str(workspace.path),
        env=env,
        label="claude",
        kind=CLAUDE,
        session_id=session_id,
        workspace_id=workspace.id,
    )
