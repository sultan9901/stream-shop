"""Google OAuth 2.0 (authorization-code flow) for viewer login."""
from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx

from app.auth.security import new_token, sign_payload, unsign_payload
from app.config import settings

log = logging.getLogger("stream.google")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
SCOPES = "openid email profile"
STATE_SALT = "google-oauth-state-v1"
STATE_MAX_AGE = 600  # 10 minutes


class GoogleAuthError(RuntimeError):
    pass


def build_state(next_url: str | None) -> str:
    return sign_payload({"n": (next_url or "/")[:300], "r": new_token(8)}, salt=STATE_SALT)


def read_state(state: str) -> str | None:
    data = unsign_payload(state, salt=STATE_SALT, max_age=STATE_MAX_AGE)
    if not data:
        return None
    nxt = str(data.get("n") or "/")
    # only allow same-site relative redirects
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


def authorization_url(state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "access_type": "online",
        "include_granted_scopes": "true",
        "prompt": "select_account",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    payload = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(TOKEN_ENDPOINT, data=payload)
    if resp.status_code != 200:
        log.warning("google token exchange failed: %s %s", resp.status_code, resp.text[:400])
        raise GoogleAuthError("Google rejected the authorization code.")
    return resp.json()


async def fetch_userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
        )
    if resp.status_code != 200:
        log.warning("google userinfo failed: %s %s", resp.status_code, resp.text[:400])
        raise GoogleAuthError("Could not read your Google profile.")
    return resp.json()


async def complete_login(code: str) -> dict:
    """Return a normalised profile dict for the authenticated Google user."""
    tokens = await exchange_code(code)
    access_token = tokens.get("access_token")
    if not access_token:
        raise GoogleAuthError("Google did not return an access token.")
    info = await fetch_userinfo(access_token)
    sub = info.get("sub")
    email = (info.get("email") or "").strip().lower()
    if not sub or not email:
        raise GoogleAuthError("Google account did not expose an email address.")
    return {
        "sub": str(sub),
        "email": email,
        "name": info.get("name") or email.split("@")[0],
        "picture": info.get("picture"),
        "locale": info.get("locale"),
        "email_verified": bool(info.get("email_verified", False)),
    }
