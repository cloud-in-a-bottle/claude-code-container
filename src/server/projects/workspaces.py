import re
import shutil
from pathlib import Path

import attr

from server.config import MIRRORS_DIR
from server.config import WORKSPACES_ROOT
from server.projects.store import PROJECT_ID_RE

# A workspace name is a directory name and a URL segment. Leading-alphanumeric rules out `..` and
# dotfiles, so a name can never escape its project's directory.
WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@attr.s(auto_attribs=True, frozen=True)
class Workspace:
    """One copy of a project's repo. Workspaces are just directories, so the list of them is read
    back off disk rather than tracked in a second file that could disagree with it."""

    project_id: str
    name: str

    @property
    def id(self) -> str:
        return f"{self.project_id}/{self.name}"

    @property
    def path(self) -> Path:
        return WORKSPACES_ROOT / self.project_id / self.name


def mirror_path(project_id: str) -> Path:
    return MIRRORS_DIR / f"{project_id}.git"


def parse_workspace_id(workspace_id: str) -> Workspace | None:
    """Parse `<project>/<workspace>`, rejecting anything that isn't a pair of safe names."""
    project_id, _, name = workspace_id.partition("/")
    if not PROJECT_ID_RE.match(project_id) or not WORKSPACE_NAME_RE.match(name):
        return None
    return Workspace(project_id=project_id, name=name)


def list_workspaces(project_id: str) -> tuple[Workspace, ...]:
    root = WORKSPACES_ROOT / project_id
    if not root.is_dir():
        return ()
    names = sorted(p.name for p in root.iterdir() if p.is_dir() and WORKSPACE_NAME_RE.match(p.name))
    return tuple(Workspace(project_id=project_id, name=n) for n in names)


def unique_workspace_name(project_id: str, base: str) -> str:
    """A free workspace name derived from `base`, which need not be a legal name itself."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._")[:56]
    if not cleaned or not WORKSPACE_NAME_RE.match(cleaned):
        cleaned = "workspace"
    taken = {w.name for w in list_workspaces(project_id)}
    if cleaned not in taken:
        return cleaned
    for n in range(2, 1000):
        candidate = f"{cleaned}-{n}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"could not find a free workspace name for {base!r} in {project_id}")


def create_workspace_dir(workspace: Workspace) -> None:
    """Make the (empty) workspace directory.

    Created up front rather than by the clone, so the workspace appears in the sidebar the moment
    it's asked for instead of when the network gets around to it. `git clone` is happy to clone
    into an existing empty directory.
    """
    workspace.path.mkdir(parents=True, exist_ok=False)


def delete_workspace(workspace: Workspace) -> None:
    shutil.rmtree(workspace.path, ignore_errors=True)
