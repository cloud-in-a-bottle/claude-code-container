import hashlib
import hmac
import time

from server.linear.config import MAX_DELIVERY_AGE_SECONDS


def signature_matches(body: bytes, header: str, secret: str) -> bool:
    """Whether `header` is Linear's HMAC-SHA256 of the raw request body.

    The raw bytes matter: re-serialising the parsed JSON would change the digest. An empty secret
    never matches, so a workbench with no `LINEAR_WEBHOOK_SECRET` configured refuses every delivery
    rather than accepting all of them.
    """
    if not secret or not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def delivery_is_fresh(webhook_timestamp_ms: int, now: float | None = None) -> bool:
    """Whether a delivery is recent enough to act on.

    A signature is valid forever, so this is what stops a captured delivery from being replayed
    later. Future timestamps are allowed the same slack as past ones, to tolerate clock skew.
    """
    if webhook_timestamp_ms <= 0:
        return False
    age = (now if now is not None else time.time()) - webhook_timestamp_ms / 1000
    return abs(age) <= MAX_DELIVERY_AGE_SECONDS
