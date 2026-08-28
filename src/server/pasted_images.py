import secrets
import time
from pathlib import Path

import attr

from server.config import STATE_DIR

# Pasted images are kept outside the workspaces on purpose: pasting a screenshot should never leave
# an untracked file in the user's repo. Claude Code is handed an absolute path, so it doesn't care
# where they live.
PASTED_IMAGES_DIR = STATE_DIR / "pasted-images"

MAX_IMAGE_BYTES = 25 * 1024 * 1024
# A path that has been pasted into a prompt has to keep resolving for as long as that conversation
# might be resumed -- but the directory can't grow forever either.
KEEP_FOR_SECONDS = 7 * 24 * 60 * 60


@attr.s(auto_attribs=True, frozen=True)
class ImageKind:
    """One of the formats Claude Code will attach when it is given the path to a file in it."""

    media_type: str
    extension: str


def sniff_image(data: bytes) -> ImageKind | None:
    """The format of `data` read off its magic bytes, or None if it isn't an image we can use.

    The content type the browser declared is deliberately ignored. What decides whether the paste
    is going to work is whether Claude Code recognises the file, and it goes by content too.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ImageKind(media_type="image/png", extension="png")
    if data.startswith(b"\xff\xd8\xff"):
        return ImageKind(media_type="image/jpeg", extension="jpg")
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ImageKind(media_type="image/gif", extension="gif")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ImageKind(media_type="image/webp", extension="webp")
    return None


def prune_pasted_images(older_than: float) -> None:
    """Delete pasted images last written before `older_than`, a unix timestamp."""
    if not PASTED_IMAGES_DIR.is_dir():
        return
    for path in PASTED_IMAGES_DIR.iterdir():
        if path.is_file() and path.stat().st_mtime < older_than:
            path.unlink(missing_ok=True)


def save_pasted_image(data: bytes, kind: ImageKind) -> Path:
    """Write one pasted image and return its path, sweeping up any that have aged out."""
    PASTED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    prune_pasted_images(time.time() - KEEP_FOR_SECONDS)
    # The name ends up typed into a prompt, so it stays short and free of anything needing quoting.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = PASTED_IMAGES_DIR / f"paste-{stamp}-{secrets.token_hex(3)}.{kind.extension}"
    path.write_bytes(data)
    return path
