"""Validated, hardened file uploads (payment screenshots, product media/files).

Rules
-----
* size ceiling enforced while streaming (never buffer an unbounded body)
* extension allowlist **and** magic-byte sniffing (a .exe renamed .png is rejected)
* images additionally decoded by Pillow to prove they are real images
* stored under a random name in ``uploads/`` — outside the static web root
* original filename kept only as metadata, never used as a path
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.auth.security import new_token
from app.config import UPLOAD_ROOT, settings

log = logging.getLogger("stream.uploads")

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".gz", ".tar", ".exe", ".msi", ".apk", ".dmg", ".pdf", ".iso"}

MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"BM": "image/bmp",
    b"PK\x03\x04": "application/zip",
    b"Rar!\x1a\x07": "application/x-rar-compressed",
    b"7z\xbc\xaf\x27\x1c": "application/x-7z-compressed",
    b"\x1f\x8b": "application/gzip",
    b"MZ": "application/x-msdownload",
    b"%PDF": "application/pdf",
}
CHUNK = 1024 * 256


@dataclass(slots=True)
class StoredFile:
    stored_path: str          # relative to UPLOAD_ROOT
    original_name: str
    content_type: str | None
    size_bytes: int
    checksum_sha256: str
    width: int | None = None
    height: int | None = None

    @property
    def absolute(self) -> Path:
        return UPLOAD_ROOT / self.stored_path


def _too_big(mb: int) -> HTTPException:
    return HTTPException(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File is larger than the {mb} MB limit."
    )


def _sniff(head: bytes) -> str | None:
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    for sig, mime in MAGIC.items():
        if head.startswith(sig):
            return mime
    return None


async def _stream_to_disk(upload: UploadFile, dest: Path, max_mb: int) -> tuple[int, str, bytes]:
    limit = max_mb * 1024 * 1024
    digest = hashlib.sha256()
    total = 0
    head = b""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise _too_big(max_mb)
                if not head:
                    head = chunk[:32]
                digest.update(chunk)
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        log.exception("upload write failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not store the file.") from exc
    if total == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty.")
    return total, digest.hexdigest(), head


def _safe_ext(filename: str | None, allowed: set[str]) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in allowed:
        nice = ", ".join(sorted(allowed))
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unsupported file type '{ext or 'unknown'}'. Allowed: {nice}"
        )
    return ext


async def save_image(
    upload: UploadFile, *, folder: str, max_mb: int | None = None
) -> StoredFile:
    max_mb = max_mb or settings.max_image_mb
    ext = _safe_ext(upload.filename, IMAGE_EXT)
    rel = f"{folder}/{new_token(16)}{ext}"
    dest = UPLOAD_ROOT / rel
    size, checksum, head = await _stream_to_disk(upload, dest, max_mb)

    mime = _sniff(head)
    if mime is None or not mime.startswith("image/"):
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That file is not a valid image (content check failed)."
        )

    width = height = None
    try:
        from PIL import Image

        with Image.open(dest) as im:
            im.verify()
        with Image.open(dest) as im:
            width, height = im.size
    except ImportError:  # pragma: no cover - Pillow optional at runtime
        log.warning("Pillow unavailable — skipping deep image validation")
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The image could not be decoded. Please upload a real screenshot."
        ) from None

    return StoredFile(
        stored_path=rel,
        original_name=(upload.filename or "screenshot")[:255],
        content_type=mime,
        size_bytes=size,
        checksum_sha256=checksum,
        width=width,
        height=height,
    )


async def save_screenshot(upload: UploadFile) -> StoredFile:
    return await save_image(upload, folder="screenshots", max_mb=settings.max_screenshot_mb)


async def save_product_file(upload: UploadFile) -> StoredFile:
    ext = _safe_ext(upload.filename, ARCHIVE_EXT | IMAGE_EXT)
    rel = f"products/{new_token(20)}{ext}"
    dest = UPLOAD_ROOT / rel
    size, checksum, head = await _stream_to_disk(upload, dest, settings.max_product_file_mb)
    return StoredFile(
        stored_path=rel,
        original_name=(upload.filename or "product")[:255],
        content_type=_sniff(head) or upload.content_type or "application/octet-stream",
        size_bytes=size,
        checksum_sha256=checksum,
    )


def resolve(stored_path: str) -> Path:
    """Resolve a stored relative path, refusing anything that escapes UPLOAD_ROOT."""
    target = (UPLOAD_ROOT / stored_path).resolve()
    root = UPLOAD_ROOT.resolve()
    if not str(target).startswith(str(root)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid file path.")
    return target


def delete(stored_path: str | None) -> None:
    if not stored_path:
        return
    try:
        resolve(stored_path).unlink(missing_ok=True)
    except Exception:  # pragma: no cover
        log.warning("could not delete %s", stored_path)
