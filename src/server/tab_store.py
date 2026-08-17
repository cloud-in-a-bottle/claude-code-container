import json

import attr

from server.config import HOME

# Under $HOME rather than /app: openhost points HOME at the app's persistent data dir, so the tab
# list survives image rebuilds. Anything written into /app does not.
TABS_PATH = HOME / ".workbench" / "tabs.json"

# What a restore should bring a tab back as. These are written to disk, so they're a file format.
CLAUDE = "claude"
SHELL = "shell"


@attr.s(auto_attribs=True, frozen=True)
class PersistedTab:
    id: str
    label: str
    kind: str
    cwd: str
    # The Claude conversation this tab owns. Pinned at launch with --session-id so a restore can
    # reattach to this exact conversation instead of whichever one was last used in the directory.
    # Empty for shell tabs, and for any tab file written without one.
    session_id: str = ""


def load_tabs() -> list[PersistedTab]:
    """Read the saved tab list, or [] when there isn't a usable one.

    Unlike the UI settings, a bad file here must not raise: restore_tabs() runs during startup, so
    failing would leave the user with a dead workbench and no terminal to repair it from. Losing
    the tab list is the smaller loss. Unparseable entries are skipped individually for the same
    reason.
    """
    if not TABS_PATH.exists():
        return []
    try:
        raw = json.loads(TABS_PATH.read_text())
    except (OSError, ValueError) as e:
        print(f"[tabs] ignoring unreadable tab list at {TABS_PATH}: {e}", flush=True)
        return []
    if not isinstance(raw, list):
        print(f"[tabs] ignoring malformed tab list at {TABS_PATH}: expected a list", flush=True)
        return []

    loaded: list[PersistedTab] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        tab_id = str(entry.get("id", ""))
        if not tab_id:
            continue
        loaded.append(
            PersistedTab(
                id=tab_id,
                label=str(entry.get("label", "term")),
                # Anything that isn't a Claude tab restores as a shell, matching restore_command().
                kind=CLAUDE if entry.get("kind") == CLAUDE else SHELL,
                cwd=str(entry.get("cwd", HOME)),
                session_id=str(entry.get("session_id", "")),
            )
        )
    return loaded


def save_tabs(tabs: list[PersistedTab]) -> None:
    """Persist via a temp file + rename, so an interrupted write can't corrupt the list."""
    TABS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = [{"id": t.id, "label": t.label, "kind": t.kind, "cwd": t.cwd, "session_id": t.session_id} for t in tabs]
    tmp_path = TABS_PATH.with_name(TABS_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
    tmp_path.replace(TABS_PATH)
