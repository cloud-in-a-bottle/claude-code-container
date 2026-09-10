import re

import attr

from server.linear.config import TRIGGER_PHRASE
from server.routes.common import JsonDict


@attr.s(auto_attribs=True, frozen=True)
class CommentEvent:
    comment_id: str
    body: str
    author_id: str
    issue_id: str
    # The human-facing key, e.g. `ENG-123`. Used for the branch name and the workspace name.
    issue_identifier: str
    issue_title: str
    delivery_timestamp_ms: int


def _trigger_pattern() -> re.Pattern[str]:
    """Match the trigger as plain text *or* inside Linear's `@[Name](id)` mention markup.

    Linear only produces real mention markup for a user that exists; typing `@claude` with no such
    user leaves the literal text. Both spellings mean the same thing to a reader, so both count.
    """
    name = re.escape(TRIGGER_PHRASE.lstrip("@"))
    return re.compile(rf"@\[?{name}\b", re.IGNORECASE)


TRIGGER_RE = _trigger_pattern()


def mentions_trigger(body: str) -> bool:
    return TRIGGER_RE.search(body) is not None


def instructions_from(body: str) -> str:
    """The comment with the trigger removed — whatever else you wrote is extra instruction."""
    return TRIGGER_RE.sub("", body, count=1).strip()


def parse_comment_event(payload: JsonDict) -> CommentEvent | None:
    """Read a Linear `Comment` webhook, or None when the payload isn't one we act on.

    Deliberately tolerant about *shape* (Linear has moved fields between `data.user` and
    `data.actor` across payload versions) and strict about *content*: anything missing an author,
    an issue or a body is dropped rather than guessed at, because every one of those is used to
    decide whether to run and what to run on.
    """
    if payload.get("type") != "Comment" or payload.get("action") != "create":
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    issue = data.get("issue")
    if not isinstance(issue, dict):
        return None

    author = data.get("user") if isinstance(data.get("user"), dict) else data.get("actor")
    author_id = str(data.get("userId") or "")
    if not author_id and isinstance(author, dict):
        author_id = str(author.get("id") or "")

    body = str(data.get("body") or "")
    issue_id = str(issue.get("id") or "")
    if not (author_id and body and issue_id):
        return None

    return CommentEvent(
        comment_id=str(data.get("id") or ""),
        body=body,
        author_id=author_id,
        issue_id=issue_id,
        issue_identifier=str(issue.get("identifier") or ""),
        issue_title=str(issue.get("title") or ""),
        delivery_timestamp_ms=int(payload.get("webhookTimestamp") or 0),
    )
