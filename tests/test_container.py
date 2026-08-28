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


def test_a_hangup_from_inside_does_not_take_the_container_down(stack: OpenhostStack) -> None:
    """`kill -HUP 1` has to be survivable, because this container invites code that can send it.

    Claude, the user's shells and this very test suite all run inside it as root, and tini installs
    no SIGHUP handler of its own — so pid 1 used to die by default action, taking every terminal in
    every workspace with it. The entrypoint ignores SIGHUP before exec'ing tini; this checks that
    the container is still the same one afterwards, not a restarted replacement.
    """
    # The harness has no public handle on the container, so the name is rebuilt the way openhost
    # builds it: openhost-<the manifest's app name>.
    manifest = tomllib.loads((Path(__file__).parent.parent / "openhost.toml").read_text())
    container = f"openhost-{manifest['app']['name']}"
    started_at = _podman("inspect", container, "--format", "{{.State.StartedAt}}")

    _podman("exec", container, "kill", "-HUP", "1")
    time.sleep(3)

    assert httpx.get(f"{stack.app_url}/health").json() == {"status": "ok"}
    assert _podman("inspect", container, "--format", "{{.State.StartedAt}}") == started_at
