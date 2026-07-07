import importlib
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).parent


def _manifest() -> dict[str, Any]:
    with open(REPO / "openhost.toml", "rb") as f:
        return tomllib.load(f)


def test_chisel_path_is_public() -> None:
    # The external chisel client has no openhost session cookie, so the tunnel endpoint must be a
    # public path. Without this, the router bounces the handshake to /login and ssh never connects.
    manifest = _manifest()
    assert "/_chisel" in manifest["routing"]["public_paths"]


def test_ssh_secret_grants_present() -> None:
    # tunnel.sh reads these from secrets-v2; they must be granted in the manifest or the fetch 403s.
    manifest = _manifest()
    granted = {
        g["key"]
        for consume in manifest["services"]["v2"]["consumes"]
        if consume.get("shortname") == "secrets"
        for g in consume["grants"]
        if "key" in g
    }
    assert {"SSH_AUTHORIZED_KEYS", "CHISEL_AUTH"} <= granted


def test_bind_host_defaults_public_and_honors_env() -> None:
    # Standalone `python server.py` must stay reachable (0.0.0.0); behind chisel, tunnel.sh pins it
    # to loopback via BIND_HOST so the backend isn't exposed except through the front-door.
    import config

    os.environ.pop("BIND_HOST", None)
    importlib.reload(config)
    assert config.BIND_HOST == "0.0.0.0"

    os.environ["BIND_HOST"] = "127.0.0.1"
    try:
        importlib.reload(config)
        assert config.BIND_HOST == "127.0.0.1"
    finally:
        os.environ.pop("BIND_HOST", None)
        importlib.reload(config)


def test_shell_scripts_have_valid_syntax() -> None:
    for script in ("tunnel.sh", "entrypoint.sh", "workbench.sh", "scripts/ssh-connect.sh"):
        subprocess.run(["bash", "-n", str(REPO / script)], check=True)


def test_sshd_is_key_only_and_loopback() -> None:
    cfg = (REPO / "sshd_config").read_text()
    assert "PasswordAuthentication no" in cfg
    assert "PermitRootLogin prohibit-password" in cfg
    assert "ListenAddress 127.0.0.1" in cfg
