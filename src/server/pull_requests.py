import asyncio
import re
import time
import urllib.parse

import attr
import httpx

from server.git_remote import is_github
from server.projects.store import Project
from server.projects.store import load_projects
from server.remote_services import fetch_github_token

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

# How often the snapshot is rebuilt. PR state moves on human timescales -- someone opens one, CI
# runs, someone merges -- so this is slow on purpose. The status route never waits on it.
REFRESH_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 15.0

# One page covers every branch anyone is likely to have a workspace for. A PR older than this drops
# out of the snapshot and its workspace reads as having no PR, which is the same thing the sidebar
# shows for a branch nobody has opened one for.
PAGE_SIZE = 100

# `owner/name` out of any GitHub clone URL: https, ssh, or scp-style.
_REPO_RE = re.compile(r"^/?([^/]+)/([^/]+?)(?:\.git)?/?$")

_QUERY = """
query($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: $first, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { number headRefName state isDraft title url }
    }
  }
}
"""

OPEN = "open"
DRAFT = "draft"
MERGED = "merged"
CLOSED = "closed"

# Keyed by repo URL, because that is what a project has and what the branches belong to. Two
# projects on the same repo share the answer rather than each paying for it.
_snapshots: dict[str, RepoPullRequests] = {}


@attr.s(auto_attribs=True, frozen=True)
class PullRequest:
    number: int
    branch: str
    state: str  # open | draft | merged | closed
    title: str
    url: str


@attr.s(auto_attribs=True, frozen=True)
class RepoPullRequests:
    """Every pull request a repo has, indexed by the branch it comes from."""

    fetched_at: float
    by_branch: dict[str, PullRequest]


def repo_slug(url: str) -> tuple[str, str] | None:
    """The `owner`, `name` pair in a GitHub clone URL, or None if it isn't one."""
    if not is_github(url):
        return None
    path = url.split(":", 1)[1] if re.match(r"^[A-Za-z0-9._-]+@", url) else urllib.parse.urlparse(url).path
    match = _REPO_RE.match(path)
    return (match.group(1), match.group(2)) if match else None


def parse_nodes(nodes: list[dict[str, object]]) -> dict[str, PullRequest]:
    """Index GitHub's PR nodes by head branch, newest first so a reopened branch wins.

    A branch can carry several PRs over its life -- opened, closed, opened again. The query asks
    for them newest-updated first, so the first one seen for a branch is the one that describes it
    now, and the rest are history.
    """
    by_branch: dict[str, PullRequest] = {}
    for node in nodes:
        branch = str(node.get("headRefName", ""))
        if not branch or branch in by_branch:
            continue
        state = str(node.get("state", "")).lower()
        if state == OPEN and node.get("isDraft"):
            state = DRAFT
        if state not in (OPEN, DRAFT, MERGED, CLOSED):
            continue
        number = node.get("number")
        by_branch[branch] = PullRequest(
            number=number if isinstance(number, int) else 0,
            branch=branch,
            state=state,
            title=str(node.get("title", "")),
            url=str(node.get("url", "")),
        )
    return by_branch


async def fetch_repo(repo_url: str, token: str) -> dict[str, PullRequest] | None:
    """Ask GitHub for a repo's pull requests. None when the answer can't be had."""
    slug = repo_slug(repo_url)
    if slug is None or not token:
        return None
    owner, name = slug
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                GITHUB_GRAPHQL_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"query": _QUERY, "variables": {"owner": owner, "name": name, "first": PAGE_SIZE}},
            )
        if response.status_code != 200:
            return None
        body = response.json()
    except httpx.HTTPError, ValueError:
        return None

    repository = (body.get("data") or {}).get("repository")
    if not isinstance(repository, dict):
        # A repo the token can't see, or a GraphQL error. Either way there is nothing to show, and
        # the caller keeps whatever it had rather than blanking the rail on one bad response.
        return None
    nodes = ((repository.get("pullRequests") or {}).get("nodes")) or []
    return parse_nodes([n for n in nodes if isinstance(n, dict)])


def pull_request_for(repo_url: str, branch: str) -> PullRequest | None:
    """The PR a workspace's branch has, from the last snapshot. Never touches the network."""
    snapshot = _snapshots.get(repo_url)
    if snapshot is None or not branch:
        return None
    return snapshot.by_branch.get(branch)


async def refresh(projects: tuple[Project, ...]) -> None:
    """Rebuild the snapshot for every GitHub project, one repo at a time.

    A repo that fails keeps its previous snapshot: a rate limit or a dropped connection should
    leave the sidebar as it was, not turn every reviewed workspace back into an unreviewed one.
    """
    token = await fetch_github_token()
    if not token:
        return
    for repo_url in dict.fromkeys(p.repo_url for p in projects):
        fetched = await fetch_repo(repo_url, token)
        if fetched is not None:
            _snapshots[repo_url] = RepoPullRequests(fetched_at=time.time(), by_branch=fetched)


async def refresh_periodically() -> None:
    """Keep the snapshot current for the life of the process."""
    while True:
        try:
            await refresh(load_projects())
        except Exception as e:  # noqa: BLE001 - a broken poll must not end the loop
            print(f"[pull-requests] refresh failed: {e}", flush=True)
        await asyncio.sleep(REFRESH_SECONDS)
