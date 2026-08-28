import shutil

from server.config import APP_DIR
from server.projects.store import Project
from server.projects.workspaces import Workspace
from server.projects.workspaces import mirror_path
from server.remote_services import get_anthropic_key
from server.tab_store import CLAUDE
from server.tabs import ServerTab
from server.tabs import create_server_tab
from server.tabs import new_session_id

CREATE_WORKSPACE_SCRIPT = APP_DIR / "projects" / "create_workspace.sh"


async def start_workspace_tab(
    project: Project, workspace: Workspace, ref: str = "", github_token: str = ""
) -> ServerTab:
    """Open the tab that builds a fresh workspace and then hands over to Claude.

    kind=CLAUDE even though the command is a bootstrap script: a restore must re-enter the
    conversation in the finished workspace, never run the clone a second time.
    """
    session_id = new_session_id()
    env = {
        "WS_PATH": str(workspace.path),
        "WS_REPO": project.repo_url,
        "WS_MIRROR": str(mirror_path(project.id)),
        "WS_REF": ref,
        "WS_SETUP": project.setup,
        "CLAUDE_BIN": shutil.which("claude") or "claude",
        "CLAUDE_SESSION_ID": session_id,
    }
    if github_token:
        env["WS_GITHUB_TOKEN"] = github_token
    key = await get_anthropic_key()
    if key:
        env["ANTHROPIC_API_KEY"] = key

    return await create_server_tab(
        command=["bash", "-l", str(CREATE_WORKSPACE_SCRIPT)],
        cwd=str(workspace.path),
        env=env,
        label="claude",
        kind=CLAUDE,
        session_id=session_id,
        workspace_id=workspace.id,
    )
