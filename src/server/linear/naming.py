import re

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")
# Enough words to say what the work is, few enough to read at a glance in the sidebar and in
# `git branch`. The character cap is the backstop for a model that ignores the word count.
_MAX_WORDS = 5
_MAX_DESCRIPTOR_CHARS = 40
# Words that read as unfinished at the end of a name. Cutting a phrase to a fixed length lands on
# one of these often enough to be worth undoing: `add-a-doc-about-our` was a real branch name, and
# it is the reason this exists.
_TRAILING_FILLER = frozenset(
    "a an the and or of to for in on at by with from our their its this that is are be"
    " about into onto as via if when while but so".split()
)


def slugify(text: str, max_words: int = _MAX_WORDS) -> str:
    """`Add a doc about our managed spaces` -> `add-a-doc`. Empty when there are no words.

    Truncation trims back off any filler it landed on, so the name reads as a phrase that ended
    rather than one that got cut. The first word is never dropped -- a short name beats no name.
    """
    words = [w for w in _SLUG_RE.sub("-", text).strip("-").lower().split("-") if w][:max_words]
    while len(words) > 1 and words[-1] in _TRAILING_FILLER:
        words.pop()
    return "-".join(words)[:_MAX_DESCRIPTOR_CHARS].strip("-")


def descriptor_slug(descriptor: str, title: str) -> str:
    """A few words naming the task.

    `descriptor` is Claude's summary of the work, which reads far better than the title: an issue
    called "Add a doc about our managed spaces" describes itself as `add-managed-spaces-docs`
    rather than as the first five words of its own title. The title is the fallback for when no
    usable descriptor came back, so a run still gets a name.
    """
    return slugify(descriptor) or slugify(title)


def run_name(issue_identifier: str, descriptor: str, title: str = "") -> str:
    """What one run is called, e.g. `cb-295-add-managed-spaces-docs`.

    Used for both the workspace and the branch, so the sidebar entry and `git branch` read the
    same and neither has to be translated into the other.

    The issue identifier leads because Linear's own GitHub integration keys off it: a branch (and
    so a PR) carrying `CB-295` gets linked back to the issue automatically, without this app doing
    anything. Lowercased throughout, since git refs are case-sensitive but people are not.
    """
    prefix = _SLUG_RE.sub("-", issue_identifier).strip("-").lower() or "issue"
    slug = descriptor_slug(descriptor, title)
    return f"{prefix}-{slug}" if slug else prefix
