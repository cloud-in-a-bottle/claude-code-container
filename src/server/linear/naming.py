import re

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")
_SLUG_WORDS = 6


def branch_name(issue_identifier: str, title: str) -> str:
    """The branch a run works on, e.g. `eng-123-fix-the-thing`.

    The issue identifier leads because Linear's own GitHub integration keys off it: a branch (and
    so a PR) carrying `ENG-123` gets linked back to the issue automatically, without this app doing
    anything. Lowercased throughout, since git refs are case-sensitive but people are not.
    """
    prefix = _SLUG_RE.sub("-", issue_identifier).strip("-").lower() or "issue"
    words = [w for w in _SLUG_RE.sub("-", title).strip("-").lower().split("-") if w][:_SLUG_WORDS]
    slug = "-".join(words)
    return f"{prefix}-{slug}" if slug else prefix


def workspace_base_name(issue_identifier: str) -> str:
    """What the workspace is called in the sidebar. Just the issue key, so a glance at the sidebar
    reads as a list of issues being worked on."""
    return _SLUG_RE.sub("-", issue_identifier).strip("-") or "issue"
