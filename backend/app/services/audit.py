"""Audit logging — every privileged action leaves a trail."""
from __future__ import annotations

import json

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import AuditLog


async def log(
    db: AsyncSession,
    *,
    action: str,
    actor=None,
    request: Request | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    summary: str | None = None,
    meta: dict | None = None,
) -> AuditLog:
    ip = ua = session_id = None
    if request is not None:
        from app.auth.sessions import client_ip

        ip = client_ip(request)
        ua = (request.headers.get("user-agent") or "")[:800] or None

    actor_id = actor_label = actor_role = None
    if actor is not None:
        user = getattr(actor, "user", actor)
        actor_id = getattr(user, "id", None)
        actor_label = getattr(user, "label", None) or getattr(user, "username", None)
        actor_role = getattr(user, "role", None)
        s = getattr(actor, "session", None)
        session_id = getattr(s, "id", None)

    row = AuditLog(
        actor_id=actor_id,
        actor_label=actor_label,
        actor_role=actor_role,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id else None,
        summary=summary,
        meta_json=json.dumps(meta, default=str) if meta else None,
        ip=ip,
        user_agent=ua,
        session_id=session_id,
    )
    db.add(row)
    await db.flush()
    return row


async def recent(
    db: AsyncSession, *, limit: int = 100, offset: int = 0, action: str | None = None
) -> tuple[list[AuditLog], int]:
    stmt = select(AuditLog)
    count_stmt = select(func.count(AuditLog.id))
    if action:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    total = int((await db.execute(count_stmt)).scalar() or 0)
    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(min(limit, 500)).offset(max(offset, 0))
    return list((await db.execute(stmt)).scalars()), total


def serialise(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "actor": row.actor_label,
        "actor_role": row.actor_role,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "summary": row.summary,
        "meta": json.loads(row.meta_json) if row.meta_json else None,
        "ip": row.ip,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
