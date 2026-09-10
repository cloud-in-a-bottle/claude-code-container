import asyncio

import attr
import httpx

from server.linear.config import LINEAR_GRAPHQL_URL
from server.remote_services import APP_TOKEN
from server.remote_services import ROUTER_URL
from server.routes.common import JsonDict

LATCHKEY_SHORTNAME = "latchkey"
_TIMEOUT_SECONDS = 30

_viewer_id: str = ""
_viewer_lock = asyncio.Lock()


class LinearError(Exception):
    """A Linear call that did not come back with usable data.

    Raised rather than swallowed: every caller here is either deciding whether someone is allowed
    to trigger work or reporting a result back to a human, and quietly doing nothing in either case
    is worse than a loud failure in the log.
    """


@attr.s(auto_attribs=True, frozen=True)
class LinearIssue:
    id: str
    identifier: str
    title: str
    description: str
    labels: tuple[str, ...]
    url: str
    team_key: str
    comments: tuple[str, ...]


async def graphql(query: str, variables: JsonDict | None = None) -> JsonDict:
    """Run a GraphQL query against Linear, with the owner's credentials injected by latchkey.

    The key itself never reaches this app: latchkey holds it and attaches it to the outbound
    request, so the worst this process can leak is the ability to make calls while it is running.
    """
    if not ROUTER_URL or not APP_TOKEN:
        raise LinearError("OPENHOST_ROUTER_URL and OPENHOST_APP_TOKEN are required to reach latchkey")

    url = f"{ROUTER_URL}/api/services/v2/call/{LATCHKEY_SHORTNAME}/proxy/{LINEAR_GRAPHQL_URL}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {APP_TOKEN}", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        raise LinearError(f"could not reach latchkey: {e}") from e

    if resp.status_code != 200:
        raise LinearError(_explain_failure(resp))

    try:
        body = resp.json()
    except ValueError as e:
        raise LinearError(f"latchkey returned a non-JSON response: {resp.text[:200]}") from e
    if not isinstance(body, dict):
        raise LinearError(f"unexpected Linear response: {resp.text[:200]}")
    if body.get("errors"):
        raise LinearError(f"Linear rejected the query: {body['errors']}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise LinearError(f"Linear returned no data: {resp.text[:200]}")
    return data


def _explain_failure(resp: httpx.Response) -> str:
    """Turn a latchkey failure into something that says what to *do* about it.

    A missing grant and a missing Linear login both surface as a bare 403 otherwise, and they need
    completely different fixes — one is a consent click, the other is a browser login.
    """
    try:
        body = resp.json()
    except ValueError:
        return f"latchkey returned {resp.status_code}: {resp.text[:200]}"
    if not isinstance(body, dict):
        return f"latchkey returned {resp.status_code}: {resp.text[:200]}"

    error = str(body.get("error") or "")
    if error == "permission_required":
        grant_url = body.get("grant_url") or ""
        return f"latchkey has no `linear-api` grant for this app yet; approve it at {grant_url}"
    if error in ("no_credentials", "not_connected"):
        return "latchkey has no Linear account connected; connect one in the latchkey console"
    if error == "account_required":
        return f"latchkey holds several Linear accounts and needs one named: {body.get('accounts')}"
    return f"latchkey returned {resp.status_code}: {body.get('message') or error or resp.text[:200]}"


async def viewer_id() -> str:
    """The Linear user whose credentials latchkey holds — i.e. the workbench's owner.

    This is what "only my comments trigger a run" is enforced against. It is fetched rather than
    configured precisely because it can't drift: whoever's key latchkey is injecting is by
    definition the person whose Linear account this workbench acts as.
    """
    global _viewer_id
    if _viewer_id:
        return _viewer_id
    async with _viewer_lock:
        if _viewer_id:
            return _viewer_id
        data = await graphql("query { viewer { id name email } }")
        viewer = data.get("viewer")
        if not isinstance(viewer, dict) or not viewer.get("id"):
            raise LinearError("Linear did not return a viewer; cannot tell whose comments to act on")
        _viewer_id = str(viewer["id"])
        return _viewer_id


_ISSUE_QUERY = """
query Issue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    url
    team { key }
    labels { nodes { name } }
    comments(first: 50) { nodes { body createdAt user { name } } }
  }
}
"""


async def fetch_issue(issue_id: str) -> LinearIssue:
    data = await graphql(_ISSUE_QUERY, {"id": issue_id})
    issue = data.get("issue")
    if not isinstance(issue, dict):
        raise LinearError(f"no Linear issue {issue_id}")

    labels = issue.get("labels")
    label_nodes = labels.get("nodes", []) if isinstance(labels, dict) else []
    comments = issue.get("comments")
    comment_nodes = comments.get("nodes", []) if isinstance(comments, dict) else []
    team = issue.get("team")

    return LinearIssue(
        id=str(issue.get("id") or issue_id),
        identifier=str(issue.get("identifier") or ""),
        title=str(issue.get("title") or ""),
        description=str(issue.get("description") or ""),
        labels=tuple(str(n.get("name", "")) for n in label_nodes if isinstance(n, dict)),
        url=str(issue.get("url") or ""),
        team_key=str(team.get("key", "")) if isinstance(team, dict) else "",
        comments=tuple(_format_comment(n) for n in comment_nodes if isinstance(n, dict)),
    )


def _format_comment(node: JsonDict) -> str:
    user = node.get("user")
    who = str(user.get("name", "someone")) if isinstance(user, dict) else "someone"
    return f"{who}: {node.get('body', '')}"


_COMMENT_MUTATION = """
mutation Comment($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) { success }
}
"""


async def post_comment(issue_id: str, body: str) -> None:
    data = await graphql(_COMMENT_MUTATION, {"issueId": issue_id, "body": body})
    result = data.get("commentCreate")
    if not isinstance(result, dict) or not result.get("success"):
        raise LinearError(f"Linear did not accept the comment on {issue_id}")
