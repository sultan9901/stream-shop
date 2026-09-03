"""Server-side session lifecycle + cookie handling."""
from __future__ import annotations

from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.security import hash_token, new_token
from app.config import settings
from app.models.base import utcnow
from app.models.device import Session as SessionRow
from app.models.user import User

VIEWER = "viewer"
STAFF = "staff"


def cookie_name(surface: str) -> str:
    return settings.session_cookie_staff if surface == STAFF else settings.session_cookie_viewer


def ttl_hours(surface: str) -> int:
    return settings.staff_session_ttl_hours if surface == STAFF else settings.session_ttl_hours


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


async def create_session(
    db: AsyncSession,
    *,
    user: User,
    surface: str,
    request: Request,
    device_id: str | None = None,
) -> tuple[SessionRow, str]:
    """Insert a session row and return it plus the raw (cookie) token."""
    raw = new_token(32)
    row = SessionRow(
        user_id=user.id,
        token_hash=hash_token(raw),
        csrf_token=new_token(24),
        role=user.role,
        surface=surface,
        device_id=device_id,
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:800],
        expires_at=utcnow() + timedelta(hours=ttl_hours(surface)),
        last_used_at=utcnow(),
    )
    db.add(row)
    await db.flush()
    return row, raw


async def load_session(db: AsyncSession, raw_token: str, surface: str) -> SessionRow | None:
    if not raw_token:
        return None
    stmt = (
        select(SessionRow)
        .where(
            SessionRow.token_hash == hash_token(raw_token),
            SessionRow.surface == surface,
            SessionRow.revoked_at.is_(None),
        )
        .options(selectinload(SessionRow.user), selectinload(SessionRow.device))
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at <= utcnow():
        return None
    return row


async def touch_session(db: AsyncSession, session_row: SessionRow, request: Request) -> None:
    await db.execute(
        update(SessionRow)
        .where(SessionRow.id == session_row.id)
        .values(last_used_at=utcnow(), ip=client_ip(request))
    )


async def revoke_session(db: AsyncSession, session_id: str) -> None:
    await db.execute(
        update(SessionRow)
        .where(SessionRow.id == session_id, SessionRow.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def revoke_all_for_user(db: AsyncSession, user_id: str, surface: str | None = None) -> int:
    stmt = update(SessionRow).where(
        SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None)
    )
    if surface:
        stmt = stmt.where(SessionRow.surface == surface)
    res = await db.execute(stmt.values(revoked_at=utcnow()))
    return res.rowcount or 0


# --------------------------------------------------------------------------
# cookies
# --------------------------------------------------------------------------
def _cookie_kwargs(surface: str) -> dict:
    return {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "path": "/",
        "max_age": ttl_hours(surface) * 3600,
    }


def set_session_cookie(response: Response, surface: str, raw_token: str) -> None:
    response.set_cookie(cookie_name(surface), raw_token, **_cookie_kwargs(surface))


def clear_session_cookie(response: Response, surface: str) -> None:
    response.delete_cookie(cookie_name(surface), path="/")


def set_device_cookie(response: Response, signed_value: str) -> None:
    response.set_cookie(
        settings.session_cookie_device,
        signed_value,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
        max_age=400 * 24 * 3600,
    )


CSRF_COOKIE = "sc_csrf"


def set_csrf_cookie(response: Response, token: str, surface: str) -> None:
    """Readable (non-httponly) by design: the browser JS must echo it back in the
    ``X-CSRF-Token`` header. The server still compares against the session row,
    so this is a double-submit check, not a bare cookie trust."""
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
        max_age=ttl_hours(surface) * 3600,
    )


def clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(CSRF_COOKIE, path="/")
