"""Integration tests: these build the real image and run it, so they need podman.

Deselected from `just test`; run them with `just test-integration`.
"""

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


def test_index_renders_the_terminal_ui(stack: OpenhostStack) -> None:
    """A smoke test that the image's static assets and templates are where Litestar expects.

    A wrong path here is the kind of break that only shows up in a real container.
    """
    response = stack.owner_session.get(stack.url)
    assert response.status_code == 200
    assert "xterm" in response.text
