from litestar import asgi
from litestar.types import Receive
from litestar.types import Scope
from litestar.types import Send

from server.preview import proxy
from server.preview.target import Mode

# Mounts rather than routes for the same reasons as the editor's: the paths belong to whatever dev
# server is behind them, and a mount is the only handler Litestar sends both HTTP and WebSocket
# scopes to -- which hot reload needs. Owner-only like every other path, because the router gates
# everything not listed in `public_paths`.


@asgi(Mode.STRIP.mount_path, is_mount=True, copy_scope=True)
async def preview_proxy(scope: Scope, receive: Receive, send: Send) -> None:
    """Serve `/proxy/<port>/<rest>` from `localhost:<port>/<rest>` inside the container."""
    await proxy.handle(Mode.STRIP, scope, receive, send)


@asgi(Mode.KEEP.mount_path, is_mount=True, copy_scope=True)
async def preview_absproxy(scope: Scope, receive: Receive, send: Send) -> None:
    """Serve `/absproxy/<port>/<rest>` from `localhost:<port>/absproxy/<port>/<rest>`, prefix and all."""
    await proxy.handle(Mode.KEEP, scope, receive, send)
