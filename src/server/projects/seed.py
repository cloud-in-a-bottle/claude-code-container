import os

from server.config import PROJECTS_PATH
from server.projects.store import Project
from server.projects.store import save_projects

# The repos the container already knows about, offered as projects so a fresh workbench has
# something in its sidebar. Only written when there is no project file at all, so removing one of
# these is permanent rather than undone on the next restart.
SEED_PROJECTS = (
    ("openhost", "OPENHOST_REPO_URL", "https://github.com/imbue-openhost/openhost.git"),
    (
        "claude-code-container",
        "WORKBENCH_REPO_URL",
        "https://github.com/cloud-in-a-bottle/claude-code-container.git",
    ),
)


def seed_projects() -> None:
    if PROJECTS_PATH.exists():
        return
    projects = tuple(
        Project(id=project_id, name=project_id, repo_url=os.environ.get(env_var) or default)
        for project_id, env_var, default in SEED_PROJECTS
    )
    save_projects(projects)
    print(f"[projects] seeded {len(projects)} project(s) at {PROJECTS_PATH}", flush=True)
