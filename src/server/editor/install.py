import asyncio
import platform
import shutil
import tarfile
import tempfile
from pathlib import Path

import httpx

from server.editor import paths

CODE_SERVER_VERSION = "4.135.0"
_RELEASE_URL = "https://github.com/coder/code-server/releases/download/v{v}/code-server-{v}-linux-{arch}.tar.gz"
_DOWNLOAD_TIMEOUT_SECONDS = 300.0

# One download at a time: two workspaces opened at once would otherwise both fetch ~235 MB.
_install_lock = asyncio.Lock()


def _arch() -> str:
    machine = platform.machine()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise RuntimeError(f"no code-server release for {machine!r}")


def release_dir() -> Path:
    return paths.INSTALL_DIR / f"code-server-{CODE_SERVER_VERSION}-linux-{_arch()}"


def binary_path() -> Path:
    return release_dir() / "bin" / "code-server"


def is_installed() -> bool:
    return binary_path().is_file()


async def _download_and_unpack() -> None:
    url = _RELEASE_URL.format(v=CODE_SERVER_VERSION, arch=_arch())
    paths.INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[editor] fetching {url}", flush=True)

    # Unpacked into a temp dir beside the real one and moved into place, so an interrupted install
    # can't leave a half-extracted tree that looks installed to the next start.
    with tempfile.TemporaryDirectory(dir=paths.INSTALL_DIR) as staging_name:
        staging = Path(staging_name)
        archive = staging / "code-server.tar.gz"
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with archive.open("wb") as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)

        await asyncio.to_thread(_unpack, archive, staging)
        unpacked = staging / release_dir().name
        if not (unpacked / "bin" / "code-server").is_file():
            raise RuntimeError(f"{url} did not unpack to {unpacked}")
        unpacked.replace(release_dir())

    print(f"[editor] installed code-server {CODE_SERVER_VERSION} into {release_dir()}", flush=True)


def _unpack(archive: Path, into: Path) -> None:
    with tarfile.open(archive) as tar:
        # `data` refuses absolute paths, symlinks pointing outside the tree, and device files. The
        # release is trusted, but this is an archive from the network unpacked as root.
        tar.extractall(into, filter="data")


async def ensure_installed() -> Path:
    """The path to the code-server binary, fetching the release first if this is the first use.

    Raises rather than degrading: an editor that can't be installed has nothing useful to fall back
    to, and the caller turns the message into an error the user can actually read.
    """
    if is_installed():
        return binary_path()
    async with _install_lock:
        if is_installed():
            return binary_path()
        await _download_and_unpack()
    return binary_path()


def uninstall() -> None:
    """Drop the install, so the next start fetches it again. Only used when upgrading the pin."""
    shutil.rmtree(paths.INSTALL_DIR, ignore_errors=True)
