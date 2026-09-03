"""Notification / in-site chat service.

Every notification is persisted first (so the bell + chat history survive a
reload) and then pushed over WebSocket to the relevant topics.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import NotificationKind, utcnow
from app.models.notification import Notification
from app.websocket.manager import manager

log = logging.getLogger("stream.notify")

BRAND = "STREAM CORPORATION"


def _serialise(n: Notification) -> dict:
    return {
        "id": n.id,
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "icon": n.icon,
        "link": n.link,
        "is_read": n.is_read,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "payload": json.loads(n.payload_json) if n.payload_json else None,
    }


async def push(
    db: AsyncSession,
    *,
    kind: str,
    title: str,
    body: str | None = None,
    user_id: str | None = None,
    audience: str | None = None,
    icon: str = "bell",
    link: str | None = None,
    payload: dict | None = None,
    topics: list[str] | None = None,
) -> Notification:
    """Persist a notification and broadcast it. Never raises into callers."""
    row = Notification(
        user_id=user_id,
        audience=audience,
        kind=kind,
        title=title,
        body=body,
        icon=icon,
        link=link,
        payload_json=json.dumps(payload, default=str) if payload else None,
    )
    db.add(row)
    await db.flush()

    if topics is None:
        topics = []
        if user_id:
            topics.append(f"user:{user_id}")
        if audience:
            topics.append(f"role:{audience}")
    try:
        await manager.broadcast(topics, {"type": "notification", "data": _serialise(row)})
    except Exception:  # pragma: no cover - never break a transaction on push
        log.exception("websocket broadcast failed")
    return row


async def push_to_staff(db: AsyncSession, **kwargs) -> None:
    """Fan a staff alert out to all masters and (optionally) one seller."""
    seller_id = kwargs.pop("seller_id", None)
    await push(db, audience="MASTER", **kwargs)
    if seller_id:
        await push(db, user_id=seller_id, audience=None, **kwargs)


async def broadcast_wallet(db: AsyncSession, user_id: str, balance: int) -> None:
    await manager.broadcast(
        [f"user:{user_id}"], {"type": "wallet", "data": {"balance": int(balance)}}
    )


async def broadcast_stats_dirty() -> None:
    await manager.broadcast(
        ["role:MASTER", "role:SELLER"], {"type": "stats_dirty", "data": {}}
    )


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
async def list_for_user(
    db: AsyncSession, *, user_id: str, role: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    cond = Notification.user_id == user_id
    if role in ("MASTER", "SELLER"):
        cond = cond | (Notification.audience == role)
    stmt = (
        select(Notification)
        .where(cond)
        .order_by(Notification.created_at.desc())
        .limit(min(limit, 200))
        .offset(max(offset, 0))
    )
    return [_serialise(n) for n in (await db.execute(stmt)).scalars()]


async def unread_count(db: AsyncSession, *, user_id: str, role: str) -> int:
    cond = Notification.user_id == user_id
    if role in ("MASTER", "SELLER"):
        cond = cond | (Notification.audience == role)
    stmt = select(func.count(Notification.id)).where(cond, Notification.is_read.is_(False))
    return int((await db.execute(stmt)).scalar() or 0)


async def mark_read(
    db: AsyncSession, *, user_id: str, role: str, notification_id: str | None = None
) -> int:
    cond = Notification.user_id == user_id
    if role in ("MASTER", "SELLER"):
        cond = cond | (Notification.audience == role)
    stmt = update(Notification).where(cond, Notification.is_read.is_(False))
    if notification_id:
        stmt = stmt.where(Notification.id == notification_id)
    res = await db.execute(stmt.values(is_read=True, read_at=utcnow()))
    return res.rowcount or 0


# --------------------------------------------------------------------------
# canned chat messages (spec sections 21 / 31)
# --------------------------------------------------------------------------
def coins_added_message(coins: int, balance: int) -> str:
    return (
        f"{BRAND}\n\nPayment confirmed successfully.\n\n"
        f"{coins:,} Coins have been added to your wallet.\n\n"
        f"Your updated balance is:\n{balance:,} Coins"
    )


def delivery_sent_message(order_code: str) -> str:
    return (
        f"{BRAND}\n\nYour product has been successfully sent to your Gmail.\n\n"
        f"Please check your Gmail inbox.\n\nOrder ID:\n{order_code}"
    )


def delivery_failed_message(order_code: str) -> str:
    return (
        f"{BRAND}\n\nYour order is confirmed, but email delivery failed.\n\n"
        f"Please contact support.\n\nOrder ID:\n{order_code}"
    )


KINDS = NotificationKind
