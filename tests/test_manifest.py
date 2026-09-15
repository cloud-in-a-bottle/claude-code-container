from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from server.linear.config import WEBHOOK_PATH

MANIFEST_PATH = Path(__file__).parent.parent / "openhost.toml"


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return tomllib.loads(MANIFEST_PATH.read_text())


def _consumed(manifest: dict[str, Any], shortname: str) -> dict[str, Any]:
    entries = manifest.get("services", {}).get("v2", {}).get("consumes", [])
    return next(e for e in entries if e.get("shortname") == shortname)


class TestPublicPaths:
    def test_the_webhook_is_public(self, manifest: dict[str, Any]) -> None:
        """Linear can't pass the router's owner login, so its path has to be listed here. Declared
        in the manifest and served by the route, with nothing tying the two together but this."""
        assert WEBHOOK_PATH in manifest["routing"]["public_paths"]

    def test_nothing_else_is_public(self, manifest: dict[str, Any]) -> None:
        """The router matches a public path exactly or as a prefix, so an entry added carelessly
        here exposes everything under it. Every route in this app but the webhook is owner-gated,
        and that is worth failing a test over rather than discovering from outside."""
        assert manifest["routing"]["public_paths"] == [WEBHOOK_PATH]


class TestLatchkeyGrant:
    def test_the_linear_grant_names_at_least_one_permission(self, manifest: dict[str, Any]) -> None:
        """An empty permission list is silently useless: latchkey drops the grant as malformed, and
        detent would approve nothing under it anyway, since a rule approves a request only if it
        matches one of the permissions listed."""
        grants = _consumed(manifest, "latchkey")["grants"]
        linear = next(g for g in grants if g["scope"] == "linear-api")
        assert linear["permissions"], "a grant with no permissions grants no access"

    def test_the_grant_is_confined_to_linear(self, manifest: dict[str, Any]) -> None:
        """`any` as a permission is only as broad as the scope above it; `any` as the *scope* would
        hand this app the owner's every stored credential."""
        grants = _consumed(manifest, "latchkey")["grants"]
        assert [g["scope"] for g in grants] == ["linear-api"]
