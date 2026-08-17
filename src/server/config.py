import os
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/home/workbench"))
OPENHOST_DIR = Path(os.environ.get("OPENHOST_DIR", str(HOME / "openhost")))
MY_PROJECT_DIR = HOME / "my_project"
APP_DIR = Path(__file__).parent
PORT = int(os.environ.get("PORT", "5000"))
