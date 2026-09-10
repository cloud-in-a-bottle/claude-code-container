import asyncio
import json
import subprocess

import attr

_LIST_TIMEOUT_SECONDS = 30
# Comfortably above the org's current size; `gh` defaults to 30, which would silently hide repos
# from the resolver and make it choose confidently from an incomplete list.
_LIST_LIMIT = 500


class RepoListError(Exception):
    pass


@attr.s(auto_attribs=True, frozen=True)
class GithubRepo:
    name: str
    description: str
    is_private: bool


@attr.s(auto_attribs=True, frozen=True)
class OrgRepos:
    org: str
    repos: tuple[GithubRepo, ...]

    def find(self, name: str) -> GithubRepo | None:
        """Look a repo up case-insensitively — the resolver is a language model, and `Cap` coming
        back as `cap` shouldn't fail a run that picked the right repo."""
        wanted = name.strip().lower().removeprefix(f"{self.org.lower()}/")
        return next((r for r in self.repos if r.name.lower() == wanted), None)

    def clone_url(self, repo: GithubRepo) -> str:
        return f"https://github.com/{self.org}/{repo.name}.git"


async def list_org_repos(org: str) -> OrgRepos:
    """Every non-archived repo in the org, with its description, as the resolver's candidate set.

    Archived repos are excluded: an issue is never asking for work in one, and they are a large
    share of the names an LLM could otherwise pick by surface similarity.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "repo",
        "list",
        org,
        "--limit",
        str(_LIST_LIMIT),
        "--no-archived",
        "--json",
        "name,description,isPrivate",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_LIST_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        raise RepoListError(f"timed out listing repos in {org}") from None
    if proc.returncode:
        raise RepoListError(f"could not list repos in {org}: {err.decode(errors='replace').strip()}")

    try:
        raw = json.loads(out)
    except ValueError as e:
        raise RepoListError(f"gh returned unparseable repo list for {org}") from e
    if not isinstance(raw, list):
        raise RepoListError(f"gh returned an unexpected repo list for {org}")

    repos = tuple(
        GithubRepo(
            name=str(entry.get("name", "")),
            description=str(entry.get("description") or ""),
            is_private=bool(entry.get("isPrivate")),
        )
        for entry in raw
        if isinstance(entry, dict) and entry.get("name")
    )
    if not repos:
        raise RepoListError(f"{org} has no repos to choose from")
    return OrgRepos(org=org, repos=repos)
