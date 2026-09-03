"""Viewer authentication via Google OAuth 2.0 (+ a dev-only stub)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import google
from app.auth import sessions as sess
from app.auth.deps import Principal, optional_viewer, require_viewer
from app.auth.ratelimit import enforce
from app.config import settings
from app.database import get_db
from app.models.device import LoginAttempt
from app.schemas.auth import DevGoogleLoginIn, MessageOut
from app.services import accounts, audit
from app.wallet import service as wallet_service

log = logging.getLogger("stream.auth.google")
router = APIRouter(tags=["auth-viewer"])


def _login_error(message: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"/?login_error={quote(message)}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/auth/google/start")
async def google_start(request: Request, next: str = Query(default="/", max_length=300)):
    await enforce(request, "google-start", "30/5m")
    if not settings.google_enabled:
        if settings.dev_stub_enabled:
            return RedirectResponse("/?dev_login=1", status_code=status.HTTP_303_SEE_OTHER)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Google sign-in is not configured. Set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.",
        )
    return RedirectResponse(
        google.authorization_url(google.build_state(next)), status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    await enforce(request, "google-callback", "30/5m")
    if error:
        return _login_error("Google sign-in was cancelled.")
    if not code or not state:
        return _login_error("Invalid Google response.")

    next_url = google.read_state(state)
    if next_url is None:
        return _login_error("Your sign-in link expired. Please try again.")

    try:
        profile = await google.complete_login(code)
    except google.GoogleAuthError as exc:
        log.warning("google login failed: %s", exc)
        return _login_error(str(exc))

    return await _establish_viewer_session(request, db, profile, next_url)


@router.post("/auth/google/dev-login")
async def dev_login(
    payload: DevGoogleLoginIn, request: Request, db: AsyncSession = Depends(get_db)
):
    """Local development helper — disabled whenever real Google keys exist or in production."""
    if not settings.dev_stub_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not available.")
    await enforce(request, "dev-login", "20/5m")
    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter a valid email address.")
    profile = {
        "sub": f"dev-{email}",
        "email": email,
        "name": payload.name or email.split("@")[0],
        "picture": None,
        "locale": "en",
        "email_verified": True,
    }
    return await _establish_viewer_session(request, db, profile, "/", json_response=True)


async def _establish_viewer_session(
    request: Request, db: AsyncSession, profile: dict, next_url: str, *, json_response: bool = False
):
    user, created = await accounts.get_or_create_viewer(db, profile)
    if not user.is_active:
        db.add(
            LoginAttempt(
                identifier=profile["email"], surface="viewer", success=False, reason="disabled",
                ip=sess.client_ip(request),
            )
        )
        await db.commit()
        if json_response:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled.")
        return _login_error("This account has been disabled.")

    user.last_login_ip = sess.client_ip(request)
    session_row, raw = await sess.create_session(
        db, user=user, surface=sess.VIEWER, request=request
    )
    db.add(
        LoginAttempt(
            identifier=profile["email"], surface="viewer", success=True, reason=None,
            ip=sess.client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:800] or None,
        )
    )
    await audit.log(
        db, action="auth.google_login", actor=user, request=request, target_type="user",
        target_id=user.id, summary=f"{user.label} signed in with Google",
        meta={"new_account": created},
    )
    await db.commit()

    if json_response:
        response = Response(status_code=status.HTTP_200_OK, media_type="application/json")
        import json as _json

        response.body = _json.dumps(
            {"ok": True, "redirect": next_url, "csrf_token": session_row.csrf_token}
        ).encode()
        response.headers["content-length"] = str(len(response.body))
    else:
        response = RedirectResponse(next_url or "/", status_code=status.HTTP_303_SEE_OTHER)

    sess.set_session_cookie(response, sess.VIEWER, raw)
    sess.set_csrf_cookie(response, session_row.csrf_token, sess.VIEWER)
    return response


@router.post("/api/auth/viewer/logout", response_model=MessageOut)
async def viewer_logout(
    response: Response,
    principal: Principal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    await sess.revoke_session(db, principal.session.id)
    await db.commit()
    sess.clear_session_cookie(response, sess.VIEWER)
    sess.clear_csrf_cookie(response)
    return MessageOut(message="Signed out.")


@router.get("/api/auth/viewer/me")
async def viewer_me(
    principal: Principal | None = Depends(optional_viewer), db: AsyncSession = Depends(get_db)
) -> dict:
    if principal is None:
        return {
            "authenticated": False,
            "google_enabled": settings.google_enabled,
            "dev_stub": settings.dev_stub_enabled,
        }
    balance = await wallet_service.balance_of(db, principal.user.id)
    return {
        "authenticated": True,
        "user": accounts.serialise_user(principal.user, wallet_balance=balance),
        "csrf_token": principal.csrf_token,
        "google_enabled": settings.google_enabled,
        "dev_stub": settings.dev_stub_enabled,
    }
