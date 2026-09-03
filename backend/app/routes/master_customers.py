"""Master panel — dashboard statistics, customer search, settings, audit log (§40–§44, §47)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import Principal, csrf_master, require_master
from app.database import get_db
from app.models.base import Role
from app.models.order import Delivery, Order
from app.models.user import User
from app.orders import service as orders
from app.payments import service as payments
from app.schemas.admin import SettingsIn
from app.services import accounts, audit, settings_store, stats
from app.wallet import service as wallet_service

router = APIRouter(prefix="/api/master", tags=["master-system"])


# ---------------------------------------------------------------- dashboard
@router.get("/overview")
async def overview(
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=14, ge=3, le=90),
) -> dict:
    return {
        "stats": await stats.master_overview(db),
        "series": await stats.daily_series(db, days=days),
        "top_products": await stats.top_products(db),
        "recent_orders": [
            orders.serialise(o) for o in (await orders.list_orders(db, limit=8))[0]
        ],
        "pending_payments": [
            payments.serialise(p)
            for p in (await payments.list_requests(db, status="PENDING", limit=8))[0]
        ],
    }


# ---------------------------------------------------------------- customers
@router.get("/customers")
async def search_customers(
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Search by Gmail, Customer ID / code, name — or list everyone (§42)."""
    stmt = select(User).where(User.role == Role.VIEWER)
    count_stmt = select(func.count(User.id)).where(User.role == Role.VIEWER)
    if q:
        term = q.strip()
        like = f"%{term}%"
        cond = or_(
            User.email.ilike(like),
            User.display_name.ilike(like),
            User.public_code.ilike(like),
            User.id == term,
        )
        # an Order ID also resolves to its customer
        order_owner = select(Order.user_id).where(Order.order_code.ilike(like))
        cond = or_(cond, User.id.in_(order_owner))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = int((await db.execute(count_stmt)).scalar() or 0)
    rows = list(
        (
            await db.execute(
                stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
            )
        ).scalars()
    )
    out = []
    for user in rows:
        data = accounts.serialise_user(user, wallet_balance=await wallet_service.balance_of(db, user.id))
        data["order_count"] = int(
            (
                await db.execute(select(func.count(Order.id)).where(Order.user_id == user.id))
            ).scalar()
            or 0
        )
        out.append(data)
    return {"total": total, "customers": out}


@router.get("/customers/{user_id}")
async def customer_profile(
    user_id: str,
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.routes.master_review import load_viewer

    user = await load_viewer(db, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found.")

    balance = await wallet_service.balance_of(db, user.id)
    order_rows, order_total = await orders.list_orders(db, user_id=user.id, limit=50)
    pay_rows, pay_total = await payments.list_requests(db, user_id=user.id, limit=50)
    deliveries = list(
        (
            await db.execute(
                select(Delivery)
                .join(Order, Order.id == Delivery.order_id)
                .where(Order.user_id == user.id)
                .order_by(Delivery.created_at.desc())
                .limit(50)
            )
        ).scalars()
    )
    return {
        "customer": accounts.serialise_user(user, wallet_balance=balance),
        "wallet": {
            "balance": balance,
            "consistency": await wallet_service.audit_consistency(db, user.id),
        },
        "transactions": [
            wallet_service.serialise(t) for t in await wallet_service.history(db, user_id=user.id, limit=100)
        ],
        "orders": {"total": order_total, "items": [orders.serialise(o) for o in order_rows]},
        "payments": {
            "total": pay_total,
            "items": [payments.serialise(p, include_user=False) for p in pay_rows],
        },
        "deliveries": [
            {
                "id": d.id,
                "order_id": d.order_id,
                "channel": d.channel,
                "status": d.status,
                "recipient": d.email_to,
                "attempts": d.attempts,
                "error": d.last_error,
                "sent_at": d.sent_at.isoformat() if d.sent_at else None,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in deliveries
        ],
    }


# ---------------------------------------------------------------- settings
@router.get("/settings")
async def read_settings(
    _: Principal = Depends(require_master), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"settings": await settings_store.detailed(db)}


@router.put("/settings")
async def write_settings(
    payload: SettingsIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not payload.values:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No settings supplied.")
    changed = await settings_store.set_many(
        db, payload.values, actor_id=principal.user.id
    )
    await audit.log(
        db, action="settings.update", actor=principal, request=request, target_type="settings",
        summary=f"Updated {changed} setting(s)", meta={"keys": sorted(payload.values)},
    )
    await db.commit()
    return {"ok": True, "updated": changed, "settings": await settings_store.detailed(db)}


# ---------------------------------------------------------------- audit log
@router.get("/audit")
async def audit_log(
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
    action: str | None = Query(default=None, max_length=60),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await audit.recent(db, limit=limit, offset=offset, action=action)
    return {"total": total, "entries": [audit.serialise(r) for r in rows]}
