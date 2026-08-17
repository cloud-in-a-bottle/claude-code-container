import os
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/home/workbench"))
OPENHOST_DIR = Path(os.environ.get("OPENHOST_DIR", str(HOME / "openhost")))
MY_PROJECT_DIR = HOME / "my_project"
APP_DIR = Path(__file__).parent
PORT = int(os.environ.get("PORT", "5000"))
# Interface the Quart app binds. In production it runs behind the chisel front-door (tunnel.sh)
# and only needs to be reachable on loopback, so tunnel.sh sets BIND_HOST=127.0.0.1. Standalone
# (`python server.py`) keeps the default so the app is reachable from outside the process.
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
