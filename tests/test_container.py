"""Integration tests: these build the real image and run it, so they need podman.

Deselected from `just test`; run them with `just test-integration`.
"""

import subprocess
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from openhost_test_harness import OpenhostStack

pytestmark = pytest.mark.integration


def test_health_endpoint(stack: OpenhostStack) -> None:
    response = httpx.get(f"{stack.app_url}/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_renders_the_app_shell(stack: OpenhostStack) -> None:
    """A smoke test that the image's static assets and templates are where Litestar expects.

    A wrong path here is the kind of break that only shows up in a real container.
    """
    response = stack.owner_session.get(stack.url)
    assert response.status_code == 200
    assert 'id="root"' in response.text
    assert "/static/ui/bundle.js" in response.text


def test_the_frontend_bundle_is_in_the_image(stack: OpenhostStack) -> None:
    """The bundle is built by the Dockerfile's node stage, so nothing in the unit tests would
    notice if that stage stopped producing it."""
    for asset in ("/static/ui/bundle.js", "/static/ui/bundle.css"):
        response = stack.owner_session.get(f"{stack.url}{asset}")
        assert response.status_code == 200, asset
        assert response.content, asset


def _podman(*args: str) -> str:
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=60, check=True).stdout.strip()


def _container_name() -> str:
    """The harness has no public handle on the container, so rebuild the name openhost gives it."""
    manifest = tomllib.loads((Path(__file__).parent.parent / "openhost.toml").read_text())
    return f"openhost-{manifest['app']['name']}"


def test_a_hangup_from_inside_does_not_take_the_container_down(stack: OpenhostStack) -> None:
    """`kill -HUP 1` has to be survivable, because this container invites code that can send it.

    Claude, the user's shells and this very test suite all run inside it as root, and tini installs
    no SIGHUP handler of its own — so pid 1 used to die by default action, taking every terminal in
    every workspace with it. The entrypoint ignores SIGHUP before exec'ing tini; this checks that
    the container is still the same one afterwards, not a restarted replacement.
    """
    container = _container_name()
    started_at = _podman("inspect", container, "--format", "{{.State.StartedAt}}")

    # Both targets matter and they fail for different reasons: pid 1 is tini, which installs no
    # SIGHUP handler of its own, and pid 2 is the server, which took a version of this fix that
    # only masked the signal in its main thread -- enough for the mask to look right in
    # /proc/2/status while a hypercorn thread still died of it.
    for target in ("1", "2"):
        _podman("exec", container, "kill", "-HUP", target)
        time.sleep(3)
        assert httpx.get(f"{stack.app_url}/health").json() == {"status": "ok"}, f"died on pid {target}"
        assert _podman("inspect", container, "--format", "{{.State.StartedAt}}") == started_at, (
            f"restarted after SIGHUP to pid {target}"
        )


# Run inside the container against the shipped module, because this is all /proc reading and the
# shape it reads only exists on Linux.
_PROGRAM_PROBE = """
import os, pty, subprocess, time
from server.tabs import ServerTab, tab_proc_info

master, slave = pty.openpty()
proc = subprocess.Popen(
    ["bash", "-l", "-c", "sleep 300; exec bash"],
    stdin=slave, stdout=slave, stderr=slave, preexec_fn=os.setsid,
)
os.close(slave)
program = ""
deadline = time.monotonic() + 10
while time.monotonic() < deadline and not program:
    program, _cwd = tab_proc_info(ServerTab(id="p", label="p", master_fd=master, proc=proc))
    time.sleep(0.1)
os.killpg(proc.pid, 9)
print(program)
"""


def test_a_tab_reports_the_program_its_shell_is_running(stack: OpenhostStack) -> None:
    """Every tab here is `bash -l -c '<program>; exec bash'`, and that shell is not interactive.

    With no job control the program never takes the terminal for itself, so the pty's foreground
    group leads with the shell — and reading the leader's name alone reported every Claude tab,
    and every workspace still bootstrapping, as a bare shell.
    """
    program = _podman("exec", _container_name(), "python3", "-c", _PROGRAM_PROBE)
    assert program.splitlines()[-1] == "sleep"
