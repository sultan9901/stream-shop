"""In-site notification / chat feed — shared by viewers, sellers and masters."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import Principal, optional_staff, optional_viewer, verify_csrf, _unauth
from app.database import get_db
from app.notifications import service as notify
from app.schemas.commerce import MarkReadIn

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


async def any_principal(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    """Resolve whichever surface the caller is signed in on (staff wins)."""
    principal = await optional_staff(request, db)
    if principal is None:
        principal = await optional_viewer(request, db)
    if principal is None:
        raise _unauth("any")
    return principal


@router.get("")
async def feed(
    principal: Principal = Depends(any_principal),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items = await notify.list_for_user(
        db, user_id=principal.user.id, role=principal.role, limit=limit, offset=offset
    )
    unread = await notify.unread_count(db, user_id=principal.user.id, role=principal.role)
    await db.commit()
    return {"notifications": items, "unread": unread}


@router.get("/unread-count")
async def unread(
    principal: Principal = Depends(any_principal), db: AsyncSession = Depends(get_db)
) -> dict:
    count = await notify.unread_count(db, user_id=principal.user.id, role=principal.role)
    await db.commit()
    return {"unread": count}


@router.post("/read")
async def mark_read(
    payload: MarkReadIn,
    request: Request,
    principal: Principal = Depends(any_principal),
    db: AsyncSession = Depends(get_db),
) -> dict:
    verify_csrf(request, principal)
    changed = await notify.mark_read(
        db,
        user_id=principal.user.id,
        role=principal.role,
        notification_id=payload.notification_id,
    )
    await db.commit()
    return {"ok": True, "updated": changed}
