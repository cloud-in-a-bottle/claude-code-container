"""Finding the Claude conversation that already belongs to a directory.

Everything here reads Claude Code's own state, which is not a published format: transcripts live at
`~/.claude/projects/<mangled-cwd>/<session-id>.jsonl`, where the mangling replaces every character
outside `[A-Za-z0-9]` with `-`. That is an implementation detail of the CLI, so every lookup below
degrades to "no session found" rather than raising -- if a future Claude changes the layout the
workbench just goes back to opening fresh conversations, which is what it did before this existed.
"""

import re
from pathlib import Path

from server.config import HOME

CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"

# A session id is a uuid (`claude --session-id` insists on one), so transcripts written by anything
# else are ignored rather than fed back to `--resume`.
_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def transcript_dir(cwd: Path | str) -> Path:
    """Where Claude keeps the transcripts for conversations held in `cwd`."""
    return CLAUDE_PROJECTS_DIR / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def latest_session_id(cwd: Path | str) -> str:
    """The most recent conversation held in `cwd`, or "" when there isn't one.

    This is what `claude --continue` would pick, resolved here instead so the caller can pin the id
    on the tab: a tab that knows its session id restores exactly, while one relying on `--continue`
    only restores correctly as long as nothing else has since talked to Claude in that directory.
    """
    try:
        transcripts = [p for p in transcript_dir(cwd).glob("*.jsonl") if _SESSION_ID_RE.match(p.stem)]
        if not transcripts:
            return ""
        return max(transcripts, key=lambda p: p.stat().st_mtime).stem
    except OSError:
        return ""
