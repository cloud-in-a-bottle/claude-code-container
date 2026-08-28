from __future__ import annotations

import os
import time
import zlib
from pathlib import Path

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from server import app as srv
from server import pasted_images
from server.pasted_images import ImageKind
from server.pasted_images import prune_pasted_images
from server.pasted_images import save_pasted_image
from server.pasted_images import sniff_image
from server.routes import images as images_route

PNG = b"\x89PNG\r\n\x1a\n" + zlib.compress(b"not really a png body")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


@pytest.fixture(autouse=True)
def images_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".workbench" / "pasted-images"
    monkeypatch.setattr(pasted_images, "PASTED_IMAGES_DIR", path)
    return path


# ── sniffing ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "extension"),
    [(PNG, "png"), (JPEG, "jpg"), (GIF, "gif"), (WEBP, "webp")],
)
def test_recognises_the_formats_claude_code_can_attach(data: bytes, extension: str) -> None:
    kind = sniff_image(data)
    assert kind is not None
    assert kind.extension == extension


@pytest.mark.parametrize("data", [b"", b"hello", b"RIFF" + b"\x00" * 4 + b"WAVE", b"<svg></svg>"])
def test_rejects_anything_that_is_not_one_of_them(data: bytes) -> None:
    """Including formats a browser will happily put on the clipboard but Claude Code won't read."""
    assert sniff_image(data) is None


def test_the_declared_content_type_is_not_what_decides(images_dir: Path) -> None:
    with _client() as client:
        resp = client.post("/api/pasted-images", content=b"just text", headers={"Content-Type": "image/png"})
    assert resp.status_code == 415


# ── saving ─────────────────────────────────────────────────────────────────────


def test_save_writes_the_bytes_and_returns_where(images_dir: Path) -> None:
    path = save_pasted_image(PNG, ImageKind(media_type="image/png", extension="png"))
    assert path.parent == images_dir
    assert path.read_bytes() == PNG


def test_save_creates_the_directory(images_dir: Path) -> None:
    assert not images_dir.exists()
    save_pasted_image(PNG, ImageKind(media_type="image/png", extension="png"))
    assert images_dir.is_dir()


def test_two_pastes_in_the_same_second_do_not_collide(images_dir: Path) -> None:
    kind = ImageKind(media_type="image/png", extension="png")
    first = save_pasted_image(PNG, kind)
    second = save_pasted_image(PNG, kind)
    assert first != second
    assert sorted(p.name for p in images_dir.iterdir()) == sorted([first.name, second.name])


def test_the_name_needs_no_quoting_when_typed_into_a_prompt(images_dir: Path) -> None:
    """The path is pasted into Claude's prompt as-is, so a space in it would split the argument."""
    path = save_pasted_image(PNG, ImageKind(media_type="image/png", extension="png"))
    assert path.name == "".join(c for c in path.name if c.isalnum() or c in "-._")


# ── pruning ────────────────────────────────────────────────────────────────────


def test_pruning_drops_the_stale_and_keeps_the_fresh(images_dir: Path) -> None:
    images_dir.mkdir(parents=True)
    stale, fresh = images_dir / "old.png", images_dir / "new.png"
    stale.write_bytes(PNG)
    fresh.write_bytes(PNG)
    old_time = time.time() - 30 * 24 * 60 * 60
    os.utime(stale, (old_time, old_time))

    prune_pasted_images(time.time() - pasted_images.KEEP_FOR_SECONDS)

    assert not stale.exists()
    assert fresh.exists()


def test_pruning_is_fine_with_no_directory_yet(images_dir: Path) -> None:
    prune_pasted_images(time.time())


def test_saving_sweeps_up_what_has_aged_out(images_dir: Path) -> None:
    images_dir.mkdir(parents=True)
    stale = images_dir / "old.png"
    stale.write_bytes(PNG)
    old_time = time.time() - 30 * 24 * 60 * 60
    os.utime(stale, (old_time, old_time))

    save_pasted_image(PNG, ImageKind(media_type="image/png", extension="png"))
    assert not stale.exists()


# ── the route ──────────────────────────────────────────────────────────────────


def test_upload_returns_a_path_the_terminal_can_use(images_dir: Path) -> None:
    with _client() as client:
        resp = client.post("/api/pasted-images", content=PNG, headers={"Content-Type": "image/png"})
    assert resp.status_code == 200
    path = Path(resp.json()["path"])
    assert path.is_absolute()
    assert path.read_bytes() == PNG


def test_upload_names_the_file_after_the_format_claude_code_looks_for(images_dir: Path) -> None:
    """Claude Code only attaches a pasted path whose *extension* is one it knows."""
    with _client() as client:
        resp = client.post("/api/pasted-images", content=JPEG, headers={"Content-Type": "image/png"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["media_type"] == "image/jpeg"
    assert body["path"].endswith(".jpg")


def test_an_empty_body_is_a_bad_request(images_dir: Path) -> None:
    with _client() as client:
        resp = client.post("/api/pasted-images", content=b"", headers={"Content-Type": "image/png"})
    assert resp.status_code == 400


def test_an_oversized_image_is_refused_before_it_is_read(images_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejected on the declared length, so a huge paste is never buffered in memory to find out."""
    monkeypatch.setattr(images_route, "MAX_IMAGE_BYTES", len(PNG) - 1)
    with _client() as client:
        resp = client.post("/api/pasted-images", content=PNG, headers={"Content-Type": "image/png"})
    assert resp.status_code == 413
    assert not images_dir.exists()


def test_an_oversized_image_is_still_refused_when_no_length_was_declared(
    images_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunked upload arrives without a Content-Length, so the size is only known once read."""
    monkeypatch.setattr(images_route, "MAX_IMAGE_BYTES", len(PNG) - 1)
    with _client() as client:
        resp = client.post("/api/pasted-images", content=iter([PNG]), headers={"Content-Type": "image/png"})
    assert resp.status_code == 413
    assert not images_dir.exists()
