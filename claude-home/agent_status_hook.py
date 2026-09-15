#!/usr/bin/env python3
"""Report what a Claude session is doing, for the workbench's sidebar dots.

Wired into ~/.claude/settings.json as a hook on a handful of events (see install_agent_hooks in
entrypoint.sh), with the state it reports passed as argv[1]. Claude Code sends the event as JSON on
stdin; every event carries `session_id` and `cwd`, which is all the placement needs.

Two rules govern everything here, because this runs *inside the user's agent*, in the critical path
of their prompt: it must never fail loudly, and it must never be slow. A hook that raises prints
into the conversation, and one that blocks makes Claude feel broken. So the whole body is wrapped,
the exit status is always 0, and nothing here opens a socket.
"""

import json
import os
import sys
import time
from pathlib import Path

# Mirrors server.config.STATE_DIR / "agent-status". Not imported from the server: this runs as a
# hook in whatever environment the agent has, which needn't be able to import the app.
STATUS_DIR = Path(os.environ.get("HOME", "")) / ".workbench" / "agent-status"

# Enough for the card to say what the agent last said, without copying a whole reply onto disk on
# every turn.
MESSAGE_LIMIT = 200

# A session id reaches the filesystem as a name, so anything that isn't one is dropped rather than
# sanitised -- there is no legitimate event whose id needs escaping.
_ID_CHARS = set("0123456789abcdefABCDEF-")


def report(state: str, event: dict[str, object]) -> None:
    session_id = str(event.get("session_id", ""))
    if not session_id or not (8 <= len(session_id) <= 64) or not set(session_id) <= _ID_CHARS:
        return

    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    path = STATUS_DIR / f"{session_id}.json"

    # The session is over: its dot should go, not linger as whatever it was doing last.
    if state == "gone":
        path.unlink(missing_ok=True)
        return

    message = str(event.get("last_assistant_message", ""))[:MESSAGE_LIMIT]
    payload = {
        "session_id": session_id,
        "state": state,
        # Where the agent is working, which is how the server places it in a workspace. Sent by
        # Claude Code itself, so it stays right even if the agent cd'd somewhere else.
        "cwd": str(event.get("cwd", "")),
        # Claude Code appends to this at every tool call, which is the only sign of life a turn
        # gives off between its start and its end. The server uses it to tell a turn that is still
        # running from one that was interrupted, since nothing fires a hook when you press escape.
        "transcript": str(event.get("transcript_path", "")),
        "at": time.time(),
        "message": message,
    }
    # Written whole and renamed into place: the server reads this directory on a poll, and a
    # half-written file would be a parse error rather than a status.
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload))
    tmp_path.replace(path)


def main() -> None:
    try:
        state = sys.argv[1] if len(sys.argv) > 1 else "working"
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if isinstance(event, dict):
            report(state, event)
    except Exception:
        # Never make the agent's turn about this hook. A missed report costs a stale dot for a few
        # seconds; a traceback costs the user's attention.
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
