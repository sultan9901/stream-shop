"""Real-time WebSocket endpoint.

Authentication reuses the same server-side session cookies as the HTTP API, so an
unauthenticated socket can never subscribe to a user or role topic.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.auth import sessions as sess
from app.database import SessionLocal
from app.models.base import Role
from app.websocket.manager import manager, topics_for

log = logging.getLogger("stream.routes.ws")
router = APIRouter()

PING_SECONDS = 25


async def _identify(ws: WebSocket) -> tuple[str, str] | None:
    """Return ``(role, user_id)`` for the connecting socket, or None."""
    async with SessionLocal() as db:
        for surface in (sess.STAFF, sess.VIEWER):
            token = ws.cookies.get(sess.cookie_name(surface), "")
            if not token:
                continue
            row = await sess.load_session(db, token, surface)
            if row is None or row.user is None or not row.user.is_active:
                continue
            user = row.user
            if surface == sess.STAFF and user.role not in (Role.MASTER, Role.SELLER):
                continue
            if surface == sess.VIEWER and user.role != Role.VIEWER:
                continue
            if surface == sess.STAFF and user.device_lock_enabled and row.device_id is None:
                continue
            return str(user.role), user.id
    return None


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    identity = await _identify(ws)
    if identity is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    role, user_id = identity
    topics = topics_for(role, user_id)
    await manager.connect(ws, topics)
    keepalive = asyncio.create_task(_keepalive(ws))
    try:
        while True:
            message = await ws.receive_text()
            if message == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - transport level
        log.debug("websocket closed unexpectedly", exc_info=True)
    finally:
        keepalive.cancel()
        # suppress CancelledError too: it derives from BaseException, so a bare
        # `suppress(Exception)` lets the cancelled keepalive re-raise and surface
        # as a spurious "Exception in ASGI application" on every normal close.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await keepalive
        await manager.disconnect(ws)


async def _keepalive(ws: WebSocket) -> None:
    """Server-side heartbeat so idle proxies do not drop the connection."""
    try:
        while True:
            await asyncio.sleep(PING_SECONDS)
            await ws.send_text('{"type":"heartbeat"}')
    except asyncio.CancelledError:  # pragma: no cover
        raise
    except Exception:
        return
