import json

from server.config import STATE_DIR

# Under $HOME, which openhost points at the app's persistent data dir, so the choice survives a
# redeploy along with the archive it applies to.
RETENTION_PATH = STATE_DIR / "archive-retention.json"

DEFAULT_RETENTION_DAYS = 14
# 0 is how the settings page says "keep archived workspaces forever".
NEVER = 0


def _check_days(days: int) -> None:
    if days < 0:
        raise ValueError(f"archive retention must be 0 (never delete) or a positive number of days, not {days}")


def load_retention_days() -> int:
    """How many days an archived workspace is kept before it is deleted; NEVER to keep it forever.

    A malformed file raises rather than reverting to the default. Guessing here deletes work on a
    schedule nobody chose, which is the one mistake this setting must not make.
    """
    if not RETENTION_PATH.exists():
        return DEFAULT_RETENTION_DAYS
    raw = json.loads(RETENTION_PATH.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("days"), int) or isinstance(raw["days"], bool):
        raise ValueError(f'malformed archive retention at {RETENTION_PATH}: expected {{"days": <int>}}')
    days: int = raw["days"]
    _check_days(days)
    return days


def save_retention_days(days: int) -> None:
    """Persist via a temp file + rename, so an interrupted write can't corrupt the file."""
    _check_days(days)
    RETENTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = RETENTION_PATH.with_name(RETENTION_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps({"days": days}, indent=2) + "\n")
    tmp_path.replace(RETENTION_PATH)
