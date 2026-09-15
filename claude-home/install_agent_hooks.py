#!/usr/bin/env python3
"""Point ~/.claude/settings.json at the agent status hook. Run by entrypoint.sh on every start.

Only the five events below are written. Everything else in settings.json belongs to the user --
their theme, their permissions, and any hooks of their own on other events -- and is read back and
preserved rather than replaced, because this file is theirs and this is a guest in it.
"""

import json
import sys
from pathlib import Path

# Which state each event reports. SessionEnd clears the report instead of setting one: a session
# that has finished should leave no dot behind it.
STATES = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "Notification": "waiting",
    "Stop": "idle",
    "SessionEnd": "gone",
}

HOOK_SCRIPT = Path(__file__).parent / "agent_status_hook.py"


def hook_entry(state: str) -> dict[str, object]:
    # sys.executable, not `python3`: a hook runs in whatever environment the agent has, and must
    # not depend on the workbench's PATH having survived into it.
    return {"hooks": [{"type": "command", "command": f"{sys.executable} {HOOK_SCRIPT} {state}"}]}


def install(home: Path) -> Path:
    path = home / ".claude" / "settings.json"
    try:
        settings = json.loads(path.read_text())
    except OSError, ValueError:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event, state in STATES.items():
        hooks[event] = [hook_entry(state)]
    settings["hooks"] = hooks

    # Claude Code reads this file at the start of every session, so it is never left half-written.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name("settings.json.tmp")
    tmp_path.write_text(json.dumps(settings, indent=2) + "\n")
    tmp_path.replace(path)
    return path


if __name__ == "__main__":
    install(Path.home())
