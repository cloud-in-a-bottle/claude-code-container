import enum
from urllib.parse import urlsplit

import attr

# The host every upstream request is addressed to. A name rather than 127.0.0.1 because a dev server
# may listen on ::1 only (Node resolves "localhost" that way), and connecting by name tries both. It
# also passes the host checks dev servers do against DNS rebinding, which a public hostname would
# fail: Vite, for one, answers anything but localhost or an IP with a 403.
UPSTREAM_HOST = "localhost"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class Mode(enum.Enum):
    # `/proxy/<port>/foo` reaches the server as `/foo`. For servers that know nothing of the prefix.
    STRIP = "proxy"
    # `/absproxy/<port>/foo` reaches the server as `/absproxy/<port>/foo`. For servers told to live
    # under that base path (`vite --base`, Next's `basePath`), so their absolute links work too.
    KEEP = "absproxy"

    @property
    def mount_path(self) -> str:
        return f"/{self.value}"


@attr.s(auto_attribs=True, frozen=True)
class Target:
    """Which local port a request under a preview mount is for, and the path to ask it for."""

    mode: Mode
    port: int
    # Always starts with "/". Percent-encoding is passed through exactly as the browser sent it.
    upstream_path: str

    @property
    def prefix(self) -> str:
        return f"{self.mode.mount_path}/{self.port}"


def parse_target(mode: Mode, raw_path: str) -> Target | None:
    """Split `/<mount>/<port>/<rest>` into the port and what to forward, or None if it isn't one."""
    mount = mode.mount_path + "/"
    if not raw_path.startswith(mount):
        return None
    port_text, slash, remainder = raw_path[len(mount) :].partition("/")
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        return None
    port = int(port_text)
    if mode is Mode.KEEP:
        return Target(mode=mode, port=port, upstream_path=raw_path)
    return Target(mode=mode, port=port, upstream_path="/" + remainder if slash else "/")


def rewrite_location(target: Target, location: str) -> str:
    """Point a redirect from the dev server back through the proxy rather than at its own address.

    The server only knows itself as localhost:<port>, which in the browser means the user's own
    machine, so an absolute redirect to itself becomes a path. In strip mode a path also needs the
    prefix back, because the server never saw it; in keep mode it already has it.
    """
    parts = urlsplit(location)
    if parts.netloc:
        if parts.hostname not in _LOOPBACK_HOSTS or parts.port != target.port:
            return location
        location = parts._replace(scheme="", netloc="").geturl() or "/"
    if target.mode is Mode.STRIP and location.startswith("/") and not location.startswith("//"):
        return target.prefix + location
    return location
