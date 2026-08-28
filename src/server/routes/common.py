from typing import Any

from litestar import Request
from litestar import Response
from litestar.datastructures import CacheControlHeader

NO_CACHE = CacheControlHeader(no_cache=True, no_store=True, must_revalidate=True)

type JsonDict = dict[str, Any]


def error(status: int, **body: Any) -> Response[JsonDict]:
    return Response(content=body, status_code=status)


async def json_body(request: Request[Any, Any, Any]) -> JsonDict:
    """The request's JSON object, or {} when there isn't a usable one.

    Callers here treat a missing or malformed body the same as an empty one and produce their own
    error, rather than letting the framework reject it with a shape the clients don't expect.
    """
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
