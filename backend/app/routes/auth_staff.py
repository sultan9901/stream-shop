"""Master / Seller authentication (separate panels, shared backend)."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import device as device_mod
from app.auth import sessions as sess
from app.auth.deps import Principal, csrf_staff, require_staff
from app.auth.ratelimit import enforce
from app.auth.security import hash_password, needs_rehash, verify_password
from app.config import settings
from app.database import get_db
from app.models.base import Role, utcnow
from app.models.device import LoginAttempt
from app.models.user import User
from app.schemas.auth import LoginOut, MessageOut, PasswordChangeIn, StaffLoginIn
from app.services import audit

router = APIRouter(prefix="/api/auth", tags=["auth-staff"])

MAX_FAILED = 8
LOCK_MINUTES = 15
GENERIC_FAIL = "Invalid username or password."


async def _record_attempt(
    db: AsyncSession, request: Request, identifier: str, surface: str, success: bool, reason: str | None
) -> None:
    db.add(
        LoginAttempt(
            identifier=identifier[:255],
            surface=surface,
            success=success,
            reason=reason,
            ip=sess.client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:800] or None,
        )
    )
    await db.flush()


async def _login(
    *,
    role: str,
    payload: StaffLoginIn,
    request: Request,
    response: Response,
    db: AsyncSession,
) -> LoginOut:
    username = payload.username.strip()
    await enforce(request, "staff-login-ip", settings.login_rate_limit)
    await enforce(request, "staff-login-user", settings.login_rate_limit, extra=username.lower())

    user = (
        await db.execute(
            select(User).where(func.lower(User.username) == username.lower(), User.role == role)
        )
    ).scalar_one_or_none()

    if user is None:
        verify_password(payload.password, None)  # equalise timing
        await _record_attempt(db, request, username, "staff", False, "unknown_user")
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, GENERIC_FAIL)

    if user.locked_until and user.locked_until > utcnow():
        await _record_attempt(db, request, username, "staff", False, "locked")
        await db.commit()
        raise HTTPException(
            status.HTTP_423_LOCKED,
            "This account is temporarily locked after too many failed attempts. Try again later.",
        )

    if not user.is_active:
        await _record_attempt(db, request, username, "staff", False, "disabled")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled.")

    if not verify_password(payload.password, user.password_hash):
        user.failed_logins = int(user.failed_logins) + 1
        if user.failed_logins >= MAX_FAILED:
            user.locked_until = utcnow() + timedelta(minutes=LOCK_MINUTES)
            user.failed_logins = 0
        await _record_attempt(db, request, username, "staff", False, "bad_password")
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, GENERIC_FAIL)

    # ---- device binding (server-side, not IP based) ----
    claim = device_mod.read_device_claim(request, payload.device_id)
    bound, error = await device_mod.bind_or_verify(
        db, user_id=user.id, claim=claim, enforce=user.device_lock_enabled
    )
    if error:
        await _record_attempt(db, request, username, "staff", False, "device_blocked")
        await audit.log(
            db, action="auth.device_blocked", actor=user, request=request,
            target_type="user", target_id=user.id, summary=f"Blocked login from unbound device for {username}",
        )
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, error)

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    user.last_login_ip = sess.client_ip(request)

    session_row, raw = await sess.create_session(
        db, user=user, surface=sess.STAFF, request=request, device_id=bound.id if bound else None
    )
    await _record_attempt(db, request, username, "staff", True, None)
    await audit.log(
        db, action="auth.login", actor=user, request=request, target_type="user",
        target_id=user.id, summary=f"{role} {username} signed in",
        meta={"device": bound.label if bound else None},
    )
    await db.commit()

    sess.set_session_cookie(response, sess.STAFF, raw)
    sess.set_csrf_cookie(response, session_row.csrf_token, sess.STAFF)
    sess.set_device_cookie(response, device_mod.device_cookie_value(claim))

    return LoginOut(
        role=user.role,
        redirect="/master" if user.role == Role.MASTER else "/seller",
        must_change_password=bool(user.must_change_password),
        csrf_token=session_row.csrf_token,
    )


@router.post("/master/login", response_model=LoginOut)
async def master_login(
    payload: StaffLoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> LoginOut:
    return await _login(role=Role.MASTER, payload=payload, request=request, response=response, db=db)


@router.post("/seller/login", response_model=LoginOut)
async def seller_login(
    payload: StaffLoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> LoginOut:
    return await _login(role=Role.SELLER, payload=payload, request=request, response=response, db=db)


@router.post("/staff/logout", response_model=MessageOut)
async def staff_logout(
    request: Request,
    response: Response,
    principal: Principal = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    await sess.revoke_session(db, principal.session.id)
    await audit.log(
        db, action="auth.logout", actor=principal, request=request, target_type="user",
        target_id=principal.user.id, summary=f"{principal.user.label} signed out",
    )
    await db.commit()
    sess.clear_session_cookie(response, sess.STAFF)
    sess.clear_csrf_cookie(response)
    return MessageOut(message="Signed out.")


@router.get("/staff/me")
async def staff_me(principal: Principal = Depends(require_staff), db: AsyncSession = Depends(get_db)) -> dict:
    from app.services.accounts import serialise_user

    devices = await device_mod.list_devices(db, principal.user.id)
    return {
        "user": serialise_user(principal.user),
        "csrf_token": principal.csrf_token,
        "session_expires_at": principal.session.expires_at.isoformat(),
        "devices": [
            {
                "id": d.id, "label": d.label, "is_active": d.is_active,
                "bound_at": d.bound_at.isoformat() if d.bound_at else None,
                "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
                "last_ip": d.last_ip,
            }
            for d in devices
        ],
    }


@router.post("/staff/change-password", response_model=MessageOut)
async def change_password(
    payload: PasswordChangeIn,
    request: Request,
    response: Response,
    principal: Principal = Depends(csrf_staff),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    await enforce(request, "change-password", "10/1h", extra=principal.user.id)
    user = (await db.execute(select(User).where(User.id == principal.user.id))).scalar_one()
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your current password is incorrect.")
    if payload.current_password == payload.new_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The new password must be different.")

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    # every other session for this account is invalidated
    await sess.revoke_all_for_user(db, user.id, sess.STAFF)
    new_session, raw = await sess.create_session(
        db, user=user, surface=sess.STAFF, request=request, device_id=principal.session.device_id
    )
    await audit.log(
        db, action="auth.password_change", actor=principal, request=request, target_type="user",
        target_id=user.id, summary=f"{user.label} changed their password",
    )
    await db.commit()
    sess.set_session_cookie(response, sess.STAFF, raw)
    sess.set_csrf_cookie(response, new_session.csrf_token, sess.STAFF)
    return MessageOut(message="Password updated. Other sessions were signed out.")
