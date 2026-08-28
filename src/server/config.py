import os
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/home/workbench"))
OPENHOST_DIR = Path(os.environ.get("OPENHOST_DIR", str(HOME / "openhost")))
APP_DIR = Path(__file__).parent
PORT = int(os.environ.get("PORT", "5000"))

# Everything below lives under $HOME, which openhost points at the app's persistent data dir, so
# projects, workspaces and their git history survive image rebuilds.
STATE_DIR = HOME / ".workbench"
PROJECTS_PATH = STATE_DIR / "projects.json"
# One bare mirror per project. Workspaces clone from it locally, so only the first workspace of a
# project pays for a network clone.
MIRRORS_DIR = STATE_DIR / "mirrors"
WORKSPACES_ROOT = HOME / "workspaces"
