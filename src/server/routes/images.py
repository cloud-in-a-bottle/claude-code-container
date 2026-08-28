from typing import Any

from litestar import Request
from litestar import Response
from litestar import post

from server.pasted_images import MAX_IMAGE_BYTES
from server.pasted_images import save_pasted_image
from server.pasted_images import sniff_image
from server.routes.common import JsonDict
from server.routes.common import error


def _declared_length(request: Request[Any, Any, Any]) -> int:
    """The body size the client said it was sending, or 0 when it didn't say."""
    raw = request.headers.get("content-length") or "0"
    return int(raw) if raw.isdigit() else 0


@post("/api/pasted-images", status_code=200)
async def create_pasted_image(request: Request[Any, Any, Any]) -> Response[JsonDict]:
    """Store an image pasted in the browser, and hand back the path it now has in the container.

    The browser's clipboard and the container's are two different machines' clipboards: Claude Code
    shells out to `xclip` here and finds no X server, which is the "no image in clipboard" the user
    sees. So the page uploads the bytes instead and types this path into the terminal, which Claude
    Code reads back into an attached image.
    """
    if _declared_length(request) > MAX_IMAGE_BYTES:
        return error(413, error="too_large", message=f"image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)}MB")

    data = await request.body()
    if not data:
        return error(400, error="bad_request", message="no image data")
    if len(data) > MAX_IMAGE_BYTES:
        return error(413, error="too_large", message=f"image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)}MB")

    kind = sniff_image(data)
    if kind is None:
        return error(415, error="unsupported_media_type", message="clipboard image is not a PNG, JPEG, GIF or WebP")

    path = save_pasted_image(data, kind)
    return Response(content={"path": str(path), "media_type": kind.media_type})
