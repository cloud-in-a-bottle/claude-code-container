from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Generator
from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from server import app as srv
from server import remote_services
from server.linear.config import WEBHOOK_PATH
from server.linear.events import CommentEvent
from server.linear.events import instructions_from
from server.linear.events import mentions_trigger
from server.linear.events import parse_comment_event
from server.linear.signature import delivery_is_fresh
from server.linear.signature import signature_matches
from server.routes import linear as route

SECRET = "shhh"


def _payload(body: str = "@claude please fix this", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "Comment",
        "action": "create",
        "webhookTimestamp": int(time.time() * 1000),
        "data": {
            "id": "comment-1",
            "body": body,
            "userId": "user-owner",
            "issue": {"id": "issue-1", "identifier": "ENG-7", "title": "Fix the thing"},
        },
    }
    payload.update(overrides)
    return payload


def _signed(payload: dict[str, Any], secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload).encode()
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"linear-signature": digest, "content-type": "application/json"}


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> Generator[list[CommentEvent]]:
    """Stub the launcher, recording the events it was asked to run.

    Recorded when the coroutine is *created*, not when it is awaited, so the assertion doesn't
    depend on the detached task getting scheduled before the test client returns.
    """
    seen: list[CommentEvent] = []

    async def noop(event: CommentEvent) -> None:
        return None

    def record(event: CommentEvent) -> Any:
        seen.append(event)
        return noop(event)

    async def secret() -> str:
        return SECRET

    monkeypatch.setattr(route, "handle_comment", record)
    monkeypatch.setattr(route, "get_linear_webhook_secret", secret)
    yield seen


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


class TestSignature:
    def test_accepts_a_correct_signature(self) -> None:
        body = b'{"hello": "world"}'
        digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert signature_matches(body, digest, SECRET)

    def test_rejects_a_signature_over_different_bytes(self) -> None:
        digest = hmac.new(SECRET.encode(), b'{"hello": "world"}', hashlib.sha256).hexdigest()
        assert not signature_matches(b'{"hello": "there"}', digest, SECRET)

    def test_an_unset_secret_matches_nothing(self) -> None:
        """A workbench with no secret configured must refuse every delivery, not accept them all."""
        body = b"{}"
        assert not signature_matches(body, hmac.new(b"", body, hashlib.sha256).hexdigest(), "")

    def test_rejects_an_empty_header(self) -> None:
        assert not signature_matches(b"{}", "", SECRET)


class TestFreshness:
    def test_a_recent_delivery_is_fresh(self) -> None:
        now = time.time()
        assert delivery_is_fresh(int(now * 1000), now=now)

    def test_an_old_delivery_is_not(self) -> None:
        now = time.time()
        assert not delivery_is_fresh(int((now - 3600) * 1000), now=now)

    def test_a_missing_timestamp_is_not(self) -> None:
        assert not delivery_is_fresh(0)


class TestTriggerMatching:
    def test_plain_text_mention(self) -> None:
        assert mentions_trigger("hey @claude take this one")

    def test_linear_mention_markup(self) -> None:
        """Linear renders a real user mention as `@[Name](id)`, which reads the same to a human."""
        assert mentions_trigger("@[Claude](abc-123) take this one")

    def test_is_case_insensitive(self) -> None:
        assert mentions_trigger("@Claude please")

    def test_ignores_an_unrelated_comment(self) -> None:
        assert not mentions_trigger("I think claude would struggle with this")

    def test_the_rest_of_the_comment_becomes_instructions(self) -> None:
        assert instructions_from("@claude only touch the parser") == "only touch the parser"


class TestParsing:
    def test_reads_a_comment_event(self) -> None:
        event = parse_comment_event(_payload())
        assert event is not None
        assert (event.issue_id, event.issue_identifier, event.author_id) == ("issue-1", "ENG-7", "user-owner")

    def test_reads_the_author_from_actor_when_there_is_no_user_id(self) -> None:
        payload = _payload()
        del payload["data"]["userId"]
        payload["data"]["actor"] = {"id": "user-owner"}
        event = parse_comment_event(payload)
        assert event is not None and event.author_id == "user-owner"

    def test_ignores_an_edited_comment(self) -> None:
        assert parse_comment_event(_payload(action="update")) is None

    def test_ignores_a_non_comment_event(self) -> None:
        assert parse_comment_event(_payload(type="Issue")) is None

    def test_ignores_a_comment_with_no_issue(self) -> None:
        payload = _payload()
        del payload["data"]["issue"]
        assert parse_comment_event(payload) is None

    def test_ignores_a_comment_with_no_author(self) -> None:
        """Without an author there is no way to tell it came from the owner, so it can't be run."""
        payload = _payload()
        del payload["data"]["userId"]
        assert parse_comment_event(payload) is None


class TestWebhookRoute:
    def test_a_signed_trigger_starts_a_run(self, started: list[CommentEvent]) -> None:
        body, headers = _signed(_payload())
        resp = _client().post(WEBHOOK_PATH, content=body, headers=headers)
        assert resp.status_code == 200
        assert [e.issue_identifier for e in started] == ["ENG-7"]

    def test_an_unsigned_delivery_is_refused(self, started: list[CommentEvent]) -> None:
        resp = _client().post(WEBHOOK_PATH, content=json.dumps(_payload()).encode())
        assert resp.status_code == 401
        assert started == []

    def test_a_delivery_signed_with_the_wrong_secret_is_refused(self, started: list[CommentEvent]) -> None:
        body, headers = _signed(_payload(), secret="not-the-secret")
        resp = _client().post(WEBHOOK_PATH, content=body, headers=headers)
        assert resp.status_code == 401
        assert started == []

    def test_a_replayed_delivery_is_refused(self, started: list[CommentEvent]) -> None:
        """The signature stays valid forever, so freshness is what stops a captured POST."""
        old = _payload()
        old["webhookTimestamp"] = int((time.time() - 3600) * 1000)
        body, headers = _signed(old)
        resp = _client().post(WEBHOOK_PATH, content=body, headers=headers)
        assert resp.status_code == 400
        assert started == []

    def test_a_comment_without_the_trigger_is_ignored(self, started: list[CommentEvent]) -> None:
        body, headers = _signed(_payload(body="just thinking out loud"))
        resp = _client().post(WEBHOOK_PATH, content=body, headers=headers)
        assert resp.status_code == 200
        assert started == []

    def test_an_unparseable_body_is_refused(self, started: list[CommentEvent]) -> None:
        digest = hmac.new(SECRET.encode(), b"not json", hashlib.sha256).hexdigest()
        resp = _client().post(WEBHOOK_PATH, content=b"not json", headers={"linear-signature": digest})
        assert resp.status_code == 400
        assert started == []


class TestSecretLookup:
    def test_an_absent_secret_is_not_re_fetched_on_every_delivery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The webhook is public, so an unconfigured workbench must not turn traffic into one
        outbound secrets call per request."""
        lookups: list[list[str]] = []

        async def fetch_secrets(keys: list[str]) -> dict[str, str]:
            lookups.append(keys)
            return {}

        monkeypatch.setattr(remote_services, "fetch_secrets", fetch_secrets)
        monkeypatch.setattr(remote_services, "_linear_webhook_secret", None)
        monkeypatch.setattr(remote_services, "_linear_webhook_missing_since", 0.0)

        client = _client()
        for _ in range(3):
            assert client.post(WEBHOOK_PATH, content=b"{}").status_code == 401
        assert len(lookups) == 1
