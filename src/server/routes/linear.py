import asyncio
import json
from collections.abc import Coroutine
from typing import Any

from litestar import Request
from litestar import Response
from litestar import post

from server.linear.config import WEBHOOK_PATH
from server.linear.events import mentions_trigger
from server.linear.events import parse_comment_event
from server.linear.launcher import NotForUs
from server.linear.launcher import handle_comment
from server.linear.signature import delivery_is_fresh
from server.linear.signature import signature_matches
from server.remote_services import get_linear_webhook_secret
from server.routes.common import JsonDict
from server.routes.common import error

SIGNATURE_HEADER = "linear-signature"


@post(WEBHOOK_PATH, status_code=200)
async def linear_webhook(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Start work on an issue the owner asked Claude to pick up, by commenting on it in Linear.

    This is the only route in the app reachable without the owner's login — Linear signs its
    deliveries but cannot authenticate as anyone — so it is written to be safe when the caller is
    hostile. Nothing is parsed before the signature over the raw body checks out, a stale delivery
    is refused so a captured one can't be replayed, and the run itself only ever starts for a
    comment written by the account whose Linear credentials latchkey holds. Error replies say as
    little as possible: an unauthenticated caller learns whether they got the signature right, and
    nothing else about what is on the other side.
    """
    body = await request.body()
    secret = await get_linear_webhook_secret()
    if not signature_matches(body, request.headers.get(SIGNATURE_HEADER, ""), secret):
        if not secret:
            print("[linear] refusing a delivery: LINEAR_WEBHOOK_SECRET is not set in the secrets app", flush=True)
        return error(401, error="bad_signature")

    try:
        payload = json.loads(body)
    except ValueError:
        return error(400, error="bad_request")
    if not isinstance(payload, dict):
        return error(400, error="bad_request")

    event = parse_comment_event(payload)
    if event is None:
        return Response(content={"ok": True, "ignored": "not a new comment"})
    if not delivery_is_fresh(event.delivery_timestamp_ms):
        return error(400, error="stale_delivery")
    if not mentions_trigger(event.body):
        return Response(content={"ok": True, "ignored": "no trigger in the comment"})

    # Detached, because the work ahead — resolving a repo with an LLM, then cloning — takes far
    # longer than Linear will wait, and a delivery it considers timed out gets retried.
    asyncio.create_task(_run(event.comment_id, handle_comment(event)))  # noqa: RUF006
    return Response(content={"ok": True, "started": True})


async def _run(comment_id: str, work: Coroutine[Any, Any, None]) -> None:
    """Await a launch that nothing is going to await, so a failure is logged rather than lost.

    A bare `create_task` whose coroutine raises reports the exception only when the task object is
    garbage collected, which for this one would be long after the message that explains it.
    """
    try:
        await work
    except NotForUs as e:
        print(f"[linear] ignoring comment {comment_id}: {e}", flush=True)
    except Exception as e:
        print(f"[linear] run for comment {comment_id} failed: {e}", flush=True)
