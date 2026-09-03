"""Device binding for staff accounts (Master / Seller).

Binding is **not** IP based. It combines:

1. a client-generated persistent device id (localStorage, sent as ``X-Device-Id``
   or a form field), and
2. a server-issued, HMAC-signed httponly ``sc_device`` cookie that is planted on
   the authorised device at binding time.

Only the HMAC of the device id is persisted. Every authenticated staff request
re-validates the session's device row server side, so removing/deactivating a
device instantly kills its sessions.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_token, new_token, sign_payload, unsign_payload
from app.auth.sessions import client_ip
from app.models.base import utcnow
from app.models.device import Device

DEVICE_SALT = "stream-device-v1"
BOUND_TO_OTHER_DEVICE = "This account is already bound to another device."


@dataclass(slots=True)
class DeviceClaim:
    raw_id: str
    fingerprint: str
    platform: str | None
    user_agent: str | None
    ip: str


def read_device_claim(request: Request, form_device_id: str | None = None) -> DeviceClaim:
    """Resolve the device id from (in priority order) the signed cookie, the
    ``X-Device-Id`` header, a form field, or freshly minted."""
    from app.config import settings

    signed = request.cookies.get(settings.session_cookie_device)
    raw = None
    if signed:
        data = unsign_payload(signed, salt=DEVICE_SALT, max_age=400 * 24 * 3600)
        if data:
            raw = data.get("d")
    raw = raw or (request.headers.get("x-device-id") or form_device_id or "").strip() or None
    if not raw or len(raw) > 200:
        raw = new_token(24)

    return DeviceClaim(
        raw_id=raw,
        fingerprint=hash_token(f"device:{raw}"),
        platform=(request.headers.get("sec-ch-ua-platform") or "").strip('"')[:80] or None,
        user_agent=(request.headers.get("user-agent") or "")[:800] or None,
        ip=client_ip(request),
    )


def device_cookie_value(claim: DeviceClaim) -> str:
    return sign_payload({"d": claim.raw_id}, salt=DEVICE_SALT)


async def list_devices(db: AsyncSession, user_id: str) -> list[Device]:
    stmt = select(Device).where(Device.user_id == user_id).order_by(Device.created_at.desc())
    return list((await db.execute(stmt)).scalars())


async def active_device(db: AsyncSession, user_id: str) -> Device | None:
    stmt = select(Device).where(Device.user_id == user_id, Device.is_active.is_(True))
    return (await db.execute(stmt)).scalars().first()


async def bind_or_verify(
    db: AsyncSession, *, user_id: str, claim: DeviceClaim, enforce: bool
) -> tuple[Device | None, str | None]:
    """Return ``(device, error)``.

    * first login (or after a Master reset) -> bind this device
    * subsequent logins -> must match the bound fingerprint
    * ``enforce=False`` (device lock disabled for the account) -> record only
    """
    existing = (
        await db.execute(
            select(Device).where(
                Device.user_id == user_id, Device.fingerprint_hash == claim.fingerprint
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if enforce and not existing.is_active:
            return None, BOUND_TO_OTHER_DEVICE
        await db.execute(
            update(Device)
            .where(Device.id == existing.id)
            .values(
                last_ip=claim.ip,
                last_seen_at=utcnow(),
                user_agent=claim.user_agent,
                is_active=True,
                bound_at=existing.bound_at or utcnow(),
            )
        )
        await db.refresh(existing)
        return existing, None

    if enforce:
        current = await active_device(db, user_id)
        if current is not None:
            return None, BOUND_TO_OTHER_DEVICE

    device = Device(
        user_id=user_id,
        fingerprint_hash=claim.fingerprint,
        label=_label_for(claim),
        user_agent=claim.user_agent,
        platform=claim.platform,
        first_ip=claim.ip,
        last_ip=claim.ip,
        is_active=True,
        bound_at=utcnow(),
        last_seen_at=utcnow(),
    )
    db.add(device)
    await db.flush()
    return device, None


async def reset_devices(db: AsyncSession, user_id: str) -> int:
    """Unbind every device for an account so the next login re-binds."""
    res = await db.execute(
        update(Device).where(Device.user_id == user_id, Device.is_active.is_(True)).values(
            is_active=False
        )
    )
    return res.rowcount or 0


def _label_for(claim: DeviceClaim) -> str:
    ua = (claim.user_agent or "").lower()
    if "android" in ua:
        base = "Android"
    elif "iphone" in ua or "ipad" in ua:
        base = "iOS"
    elif "windows" in ua:
        base = "Windows"
    elif "mac os" in ua or "macintosh" in ua:
        base = "macOS"
    elif "linux" in ua:
        base = "Linux"
    else:
        base = claim.platform or "Unknown"
    browser = (
        "Edge" if "edg/" in ua else
        "Chrome" if "chrome" in ua else
        "Firefox" if "firefox" in ua else
        "Safari" if "safari" in ua else "Browser"
    )
    return f"{base} · {browser}"[:120]
