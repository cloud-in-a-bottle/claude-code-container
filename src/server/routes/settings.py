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
from server.routes.common import JsonDict
from server.routes.common import error
from server.routes.common import json_body


async def _settings_json() -> JsonDict:
    """What the settings page needs to describe the workbench's billing setup.

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
    }


@get("/api/settings")
async def get_settings() -> JsonDict:
    return await _settings_json()


@post("/api/settings", status_code=200)
async def update_settings(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Change the billing mode new workspaces are created with.

    Existing workspaces are unaffected: each is pinned to the mode it was created with, so the
    conversations already running somewhere keep being paid for the way they started.
    """
    data = await json_body(request)
    if "default_billing" not in data:
        return error(400, error="bad_request", message="expected default_billing")
    mode = str(data["default_billing"])
    if mode not in MODES:
        return error(400, error="bad_request", message=f"default_billing must be one of: {', '.join(MODES)}")

    set_default_mode(mode)
    return Response(content=await _settings_json())
