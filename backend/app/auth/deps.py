"""FastAPI auth dependencies: identity resolution, RBAC, CSRF."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import sessions as sess
from app.auth.security import constant_time_equals
from app.config import settings
from app.database import get_db
from app.models.device import Device
from app.models.device import Session as SessionRow
from app.models.user import Role, User

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


@dataclass(slots=True)
class Principal:
    user: User
    session: SessionRow
    surface: str

    @property
    def id(self) -> str:
        return self.user.id

    @property
    def role(self) -> str:
        return self.user.role

    @property
    def is_master(self) -> bool:
        return self.user.role == Role.MASTER

    @property
    def is_seller(self) -> bool:
        return self.user.role == Role.SELLER

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_token


def _unauth(surface: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
        headers={"X-Auth-Surface": surface},
    )


async def _resolve(request: Request, db: AsyncSession, surface: str) -> Principal | None:
    token = request.cookies.get(sess.cookie_name(surface), "")
    row = await sess.load_session(db, token, surface)
    if row is None or row.user is None:
        return None
    user = row.user
    if not user.is_active:
        return None

    # staff sessions are re-validated against their bound device on every request
    if surface == sess.STAFF and user.device_lock_enabled:
        if row.device_id is None:
            return None
        device = row.device or (
            await db.execute(select(Device).where(Device.id == row.device_id))
        ).scalar_one_or_none()
        if device is None or not device.is_active or device.user_id != user.id:
            return None

    await sess.touch_session(db, row, request)
    # Commit the session touch in its own tiny transaction so it persists
    # regardless of what the handler does, and — crucially on SQLite — so the
    # process-wide writer lock this UPDATE takes is released *before* the handler
    # runs and before any BackgroundTask it schedules. Holding it until session
    # close (which happens after background tasks) would deadlock a delivery task
    # against the very request that queued it.
    await db.commit()
    return Principal(user=user, session=row, surface=surface)


# --------------------------------------------------------------------------
# viewer (public website)
# --------------------------------------------------------------------------
async def optional_viewer(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Principal | None:
    p = await _resolve(request, db, sess.VIEWER)
    if p and p.user.role != Role.VIEWER:
        return None
    return p


async def require_viewer(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Principal:
    p = await optional_viewer(request, db)
    if p is None:
        raise _unauth("viewer")
    return p


# --------------------------------------------------------------------------
# staff (master / seller panels)
# --------------------------------------------------------------------------
async def optional_staff(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Principal | None:
    p = await _resolve(request, db, sess.STAFF)
    if p and p.user.role not in (Role.MASTER, Role.SELLER):
        return None
    return p


async def require_staff(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    p = await optional_staff(request, db)
    if p is None:
        raise _unauth("staff")
    return p


async def require_master(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    p = await optional_staff(request, db)
    if p is None:
        raise _unauth("master")
    if p.user.role != Role.MASTER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Master privileges required.")
    return p


async def require_seller(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    p = await optional_staff(request, db)
    if p is None:
        raise _unauth("seller")
    if p.user.role != Role.SELLER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Seller privileges required.")
    return p


# --------------------------------------------------------------------------
# CSRF — double submit, validated against the server-side session row
# --------------------------------------------------------------------------
def verify_csrf(request: Request, principal: Principal) -> None:
    if request.method in SAFE_METHODS:
        return
    sent = request.headers.get("x-csrf-token") or ""
    if not sent:
        form_token = getattr(request.state, "csrf_form_token", "")
        sent = form_token or ""
    if not constant_time_equals(sent, principal.csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid.")


async def csrf_viewer(
    request: Request, principal: Principal = Depends(require_viewer)
) -> Principal:
    verify_csrf(request, principal)
    return principal


async def csrf_staff(request: Request, principal: Principal = Depends(require_staff)) -> Principal:
    verify_csrf(request, principal)
    return principal


async def csrf_master(request: Request, principal: Principal = Depends(require_master)) -> Principal:
    verify_csrf(request, principal)
    return principal


async def csrf_seller(request: Request, principal: Principal = Depends(require_seller)) -> Principal:
    verify_csrf(request, principal)
    return principal


def public_base_url() -> str:
    return settings.base_url.rstrip("/")
