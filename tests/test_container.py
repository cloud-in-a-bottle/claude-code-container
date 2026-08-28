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
