"""Vouching for the directories the workbench itself created.

On first launch in a directory Claude asks "Is this a project you created or one you trust?", with
"No, exit" preselected. Every workbench workspace is a brand-new path, so without this every new
workspace opens onto that dialog instead of a conversation -- and the natural keypress dismisses
Claude entirely, dropping the user at a bare shell.

The workbench is in a position to answer that question: the workspace exists because the user asked
this app to clone a repo they named into a directory it created. It is not a folder they wandered
into. Note also that `claude` is launched with --dangerously-skip-permissions in this container (see
the Dockerfile on why that is the deal here), so the dialog is not what is standing between repo
contents and execution -- it just stops the terminal from being usable.

`projects[<abs path>].hasTrustDialogAccepted` in ~/.claude.json is the CLI's own documented remedy;
its error text offers it in place of accepting the dialog interactively. That file belongs to Claude
and is not a published format, so everything here is best-effort: worst case the flag doesn't take
and the user sees the dialog they would have seen anyway.
"""

import json
from pathlib import Path
from typing import Any

from server.config import HOME

CLAUDE_CONFIG_PATH = HOME / ".claude.json"


def _load_config() -> dict[str, Any] | None:
    """The parsed config, or None when it can't be read as one.

    None means "leave the file alone". This config holds the user's login and onboarding state, so
    replacing an unreadable one with a fresh minimal file would log them out to save a dialog.
    """
    try:
        raw = json.loads(CLAUDE_CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"[trust] leaving {CLAUDE_CONFIG_PATH} alone: {e}", flush=True)
        return None
    return raw if isinstance(raw, dict) else None


def is_trusted(path: Path | str) -> bool:
    config = _load_config()
    if not config:
        return False
    projects = config.get("projects")
    entry = projects.get(str(path)) if isinstance(projects, dict) else None
    return bool(entry.get("hasTrustDialogAccepted")) if isinstance(entry, dict) else False


def trust_dir(path: Path | str) -> None:
    """Record that this directory is trusted, so Claude opens straight into the conversation.

    Deliberately a no-op once the flag is set, which makes the steady state zero writes: Claude
    rewrites this file constantly from every running tab, and read-modify-write from the workbench
    is a lost update waiting to happen. Confining the write to the first Claude launch in a given
    directory keeps that window to one moment we already control.
    """
    key = str(path)
    config = _load_config()
    if config is None:
        return
    projects = config.setdefault("projects", {})
    if not isinstance(projects, dict):
        return
    entry = projects.get(key)
    if not isinstance(entry, dict):
        entry = {}
    if entry.get("hasTrustDialogAccepted"):
        return
    # Merged into whatever else Claude keeps per project rather than replacing it, so trusting a
    # directory can't discard the history or counters already recorded against it.
    projects[key] = {**entry, "hasTrustDialogAccepted": True}

    tmp_path = CLAUDE_CONFIG_PATH.with_name(CLAUDE_CONFIG_PATH.name + ".workbench.tmp")
    try:
        CLAUDE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(config, indent=2) + "\n")
        tmp_path.replace(CLAUDE_CONFIG_PATH)
    except OSError as e:
        # A terminal that opens on the trust dialog still beats no terminal.
        print(f"[trust] could not mark {key} trusted: {e}", flush=True)
        tmp_path.unlink(missing_ok=True)
