import json
import re

import attr

from server.config import PROJECTS_PATH
from server.git_remote import REF_RE

# A project id doubles as a directory name (under ~/workspaces and ~/.workbench/mirrors), so it is
# restricted to characters that are unambiguous on disk and in a URL.
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@attr.s(auto_attribs=True, frozen=True)
class Project:
    id: str
    name: str
    repo_url: str
    # Optional command run once in a new workspace before Claude starts, e.g. `just setup`.
    setup: str = ""
    # Branch new workspaces start from. Empty means "whatever the repo's own default branch is",
    # resolved from the remote at creation time rather than remembered here.
    default_branch: str = ""


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-._")
    return slug[:64] or "project"


def unique_project_id(base: str, taken: frozenset[str]) -> str:
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"could not find a free project id for {base!r}")


def load_projects() -> tuple[Project, ...]:
    """Read the configured projects.

    A malformed file raises rather than silently presenting an empty project list — losing track of
    where your work lives is exactly the kind of thing that should be loud. This is only read by
    request handlers and by the seeder (which checks existence first), so a bad file can't stop the
    workbench from booting.
    """
    if not PROJECTS_PATH.exists():
        return ()
    raw = json.loads(PROJECTS_PATH.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"malformed project list at {PROJECTS_PATH}: expected a list")

    projects: list[Project] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"malformed project list at {PROJECTS_PATH}: expected a list of objects")
        project_id = str(entry["id"])
        if not PROJECT_ID_RE.match(project_id):
            raise ValueError(f"malformed project id {project_id!r} in {PROJECTS_PATH}")
        # Re-checked on the way in, not just on the way out: this ends up on a `git` command line,
        # and the file is editable by hand.
        default_branch = str(entry.get("default_branch", ""))
        if default_branch and not REF_RE.match(default_branch):
            raise ValueError(f"malformed default branch {default_branch!r} for {project_id} in {PROJECTS_PATH}")
        projects.append(
            Project(
                id=project_id,
                name=str(entry.get("name", project_id)),
                repo_url=str(entry["repo_url"]),
                setup=str(entry.get("setup", "")),
                default_branch=default_branch,
            )
        )
    return tuple(projects)


def save_projects(projects: tuple[Project, ...]) -> None:
    """Persist via a temp file + rename, so an interrupted write can't corrupt the list."""
    PROJECTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "id": p.id,
            "name": p.name,
            "repo_url": p.repo_url,
            "setup": p.setup,
            "default_branch": p.default_branch,
        }
        for p in projects
    ]
    tmp_path = PROJECTS_PATH.with_name(PROJECTS_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
    tmp_path.replace(PROJECTS_PATH)


def find_project(project_id: str) -> Project | None:
    return next((p for p in load_projects() if p.id == project_id), None)


def find_project_by_repo(repo_url: str) -> Project | None:
    return next((p for p in load_projects() if p.repo_url == repo_url), None)


def add_project(name: str, repo_url: str, setup: str = "", default_branch: str = "") -> Project:
    projects = load_projects()
    project = Project(
        id=unique_project_id(slugify(name), frozenset(p.id for p in projects)),
        name=name.strip(),
        repo_url=repo_url,
        setup=setup.strip(),
        default_branch=default_branch.strip(),
    )
    save_projects((*projects, project))
    return project


def remove_project(project_id: str) -> None:
    save_projects(tuple(p for p in load_projects() if p.id != project_id))
