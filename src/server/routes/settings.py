from typing import Any

from litestar import Request
from litestar import Response
from litestar import get
from litestar import post

from server.billing import MODES
from server.billing import api_key_available
from server.billing import load_billing
from server.billing import set_default_mode
from server.billing import subscription_status
from server.projects.retention import load_retention_days
from server.projects.retention import save_retention_days
from server.routes.common import JsonDict
from server.routes.common import error
from server.routes.common import json_body


async def _settings_json() -> JsonDict:
    """What the settings page shows: the workbench's billing setup, and how long archives are kept.

    The subscription probe shells out to `claude auth status` on every call rather than being
    cached: this is what the page's "check again" button reads, and it is asked for once per visit
    to the page, not on a poll.
    """
    return {
        "default_billing": load_billing().default_mode,
        # Whether API billing has anything to bill *to*, so the page can say so before someone
        # creates a workspace that comes up asking to be logged in.
        "api_key_available": await api_key_available(),
        "subscription": await subscription_status(),
        "archive_retention_days": load_retention_days(),
    }


@get("/api/settings")
async def get_settings() -> JsonDict:
    return await _settings_json()


@post("/api/settings", status_code=200)
async def update_settings(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Change the billing mode new workspaces are created with, and/or how long archives are kept.

    Keys left out keep their current value. Everything is checked before anything is saved, so a
    request with one bad value changes nothing.

    Existing workspaces keep their billing mode: each is pinned to the mode it was created with, so
    the conversations already running somewhere keep being paid for the way they started.
    """
    data = await json_body(request)
    if "default_billing" not in data and "archive_retention_days" not in data:
        return error(400, error="bad_request", message="expected default_billing or archive_retention_days")

    mode = None
    if "default_billing" in data:
        mode = str(data["default_billing"])
        if mode not in MODES:
            return error(400, error="bad_request", message=f"default_billing must be one of: {', '.join(MODES)}")

    days = None
    if "archive_retention_days" in data:
        raw = data["archive_retention_days"]
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            return error(
                400, error="bad_request", message="archive_retention_days must be 0 (never) or a positive integer"
            )
        days = raw

    if mode is not None:
        set_default_mode(mode)
    if days is not None:
        save_retention_days(days)
    return Response(content=await _settings_json())
