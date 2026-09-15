from __future__ import annotations

import httpx

from server.linear.api import _explain_failure


def _resp(status: int, body: object) -> httpx.Response:
    return httpx.Response(status_code=status, json=body)


class TestExplainFailure:
    def test_a_missing_grant_points_at_the_consent_page(self) -> None:
        """A bare 403 is indistinguishable from a missing login, and needs a different fix."""
        message = _explain_failure(
            _resp(403, {"error": "permission_required", "grant_url": "https://zone/approve?x=1"})
        )
        assert "grant" in message and "https://zone/approve?x=1" in message

    def test_a_missing_linear_login_says_to_connect_one(self) -> None:
        assert "connect one" in _explain_failure(_resp(403, {"error": "no_credentials"}))

    def test_several_accounts_lists_them(self) -> None:
        message = _explain_failure(_resp(400, {"error": "account_required", "accounts": ["a@x", "b@x"]}))
        assert "a@x" in message

    def test_a_router_refusal_is_named_as_the_router(self) -> None:
        """An undeclared shortname or an unsatisfied version spec is fixed in this app's manifest,
        not in latchkey, so the message must not blame latchkey."""
        message = _explain_failure(
            _resp(503, {"status_code": 503, "detail": "Provider 'latchkey' version 0.1.0 does not match '>=0.2.0'"})
        )
        assert "router" in message
        assert "does not match" in message

    def test_a_non_json_body_is_still_reported(self) -> None:
        message = _explain_failure(httpx.Response(status_code=502, text="<html>bad gateway</html>"))
        assert "502" in message
