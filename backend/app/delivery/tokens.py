"""Secure, expiring, per-order product download grants (spec §32)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_token, new_token
from app.config import settings
from app.models.base import utcnow
from app.models.order import DownloadLog, DownloadToken


async def issue(
    db: AsyncSession,
    *,
    order_id: str,
    user_id: str,
    product_file_id: str | None,
    external_url: str | None,
    ttl_hours: int | None = None,
) -> tuple[DownloadToken, str]:
    """Create a one-customer download grant; returns the row and the raw token."""
    raw = new_token(32)
    row = DownloadToken(
        token_hash=hash_token(f"download:{raw}"),
        order_id=order_id,
        user_id=user_id,
        product_file_id=product_file_id,
        external_url=external_url,
        expires_at=utcnow() + timedelta(hours=ttl_hours or settings.download_token_ttl_hours),
        max_downloads=settings.download_max_attempts,
    )
    db.add(row)
    await db.flush()
    return row, raw


def download_url(raw_token: str) -> str:
    return f"{settings.base_url.rstrip('/')}/download/{raw_token}"


async def resolve(db: AsyncSession, raw_token: str) -> tuple[DownloadToken | None, str]:
    """Return ``(token, reason)``; ``reason`` is '' when the grant is usable."""
    if not raw_token or len(raw_token) > 200:
        return None, "invalid"
    row = (
        await db.execute(
            select(DownloadToken).where(DownloadToken.token_hash == hash_token(f"download:{raw_token}"))
        )
    ).scalar_one_or_none()
    if row is None:
        return None, "invalid"
    if row.revoked:
        return row, "revoked"
    if row.expires_at <= utcnow():
        return row, "expired"
    if row.download_count >= row.max_downloads:
        return row, "exhausted"
    return row, ""


async def register_hit(db: AsyncSession, token: DownloadToken) -> None:
    await db.execute(
        update(DownloadToken)
        .where(DownloadToken.id == token.id)
        .values(download_count=DownloadToken.download_count + 1, last_downloaded_at=utcnow())
    )


async def log_attempt(
    db: AsyncSession,
    *,
    token: DownloadToken | None,
    outcome: str,
    ip: str | None,
    user_agent: str | None,
    user_id: str | None = None,
) -> None:
    db.add(
        DownloadLog(
            token_id=token.id if token else None,
            order_id=token.order_id if token else None,
            user_id=user_id or (token.user_id if token else None),
            ip=ip,
            user_agent=(user_agent or "")[:800] or None,
            outcome=outcome,
        )
    )
    await db.flush()


async def revoke_for_order(db: AsyncSession, order_id: str) -> int:
    res = await db.execute(
        update(DownloadToken)
        .where(DownloadToken.order_id == order_id, DownloadToken.revoked.is_(False))
        .values(revoked=True)
    )
    return res.rowcount or 0
