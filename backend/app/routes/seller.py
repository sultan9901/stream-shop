"""Seller panel API (spec §7, §33) — orders assigned to this seller, plus payment
verification when the Master granted that permission.

Every query is scoped to ``seller_id == principal.user.id``; a seller can never
read or touch another seller's orders, and never reaches Master-only endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import Principal, csrf_seller, require_seller
from app.database import get_db
from app.models.base import PaymentStatus
from app.models.payment import PaymentRequest
from app.orders import service as orders
from app.payments import service as payments
from app.schemas.commerce import OrderNoteIn, PaymentReviewIn
from app.services import catalog, stats
from app.wallet import service as wallet_service

router = APIRouter(prefix="/api/seller", tags=["seller"])


def _can_verify(principal: Principal) -> bool:
    account = getattr(principal.user, "seller_account", None)
    return bool(account and account.can_verify_payments)


def _require_verify(principal: Principal) -> None:
    if not _can_verify(principal):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your account is not allowed to verify coin payments. Ask a Master to enable it.",
        )


@router.get("/overview")
async def overview(
    principal: Principal = Depends(require_seller), db: AsyncSession = Depends(get_db)
) -> dict:
    counts = await orders.status_counts(db, seller_id=principal.user.id)
    recent, _ = await orders.list_orders(db, seller_id=principal.user.id, limit=8)
    payload = {
        "stats": await stats.seller_overview(db, principal.user.id),
        "counts": {"total": sum(counts.values()), **counts},
        "recent_orders": [orders.serialise(o) for o in recent],
        "can_verify_payments": _can_verify(principal),
    }
    if _can_verify(principal):
        pending, total = await payments.list_requests(db, status=PaymentStatus.PENDING, limit=8)
        payload["pending_payments"] = [payments.serialise(p) for p in pending]
        payload["pending_payment_total"] = total
    return payload


# ---------------------------------------------------------------- orders
@router.get("/orders")
async def list_orders(
    principal: Principal = Depends(require_seller),
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status", max_length=24),
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await orders.list_orders(
        db, seller_id=principal.user.id, status=status_filter, search=q,
        limit=limit, offset=offset,
    )
    counts = await orders.status_counts(db, seller_id=principal.user.id)
    return {
        "total": total,
        "counts": {"total": sum(counts.values()), **counts},
        "orders": [orders.serialise(o) for o in rows],
    }


@router.get("/orders/{order_id}")
async def order_detail(
    order_id: str,
    principal: Principal = Depends(require_seller),
    db: AsyncSession = Depends(get_db),
) -> dict:
    order = await orders.get_order(db, order_id)
    if order is None or order.seller_id != principal.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    return {"order": orders.serialise(order)}


@router.post("/orders/{order_id}/complete")
async def complete_order(
    order_id: str,
    payload: OrderNoteIn,
    request: Request,
    principal: Principal = Depends(csrf_seller),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """MARK COMPLETED. ``mark_completed`` re-checks the assignment server-side."""
    try:
        order = await orders.mark_completed(
            db, order_id=order_id, actor=principal, note=payload.note, request=request
        )
    except orders.OrderError as exc:
        await db.rollback()
        code = {
            "not_found": status.HTTP_404_NOT_FOUND,
            "forbidden": status.HTTP_403_FORBIDDEN,
        }.get(exc.code, status.HTTP_400_BAD_REQUEST)
        raise HTTPException(code, {"error": exc.code, "message": str(exc)}) from exc
    await db.commit()
    return {
        "ok": True,
        "message": f"{order.order_code} marked completed.",
        "order": orders.serialise(order),
    }


# ---------------------------------------------------------------- products
@router.get("/products")
async def my_products(
    principal: Principal = Depends(require_seller),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await catalog.list_products(
        db, only_active=False, seller_id=principal.user.id, sort="newest",
        limit=limit, offset=offset, with_media=True,
    )
    return {
        "total": total,
        "products": [catalog.serialise_product(p, staff=True) for p in rows],
    }


# ---------------------------------------------------------------- payment review
@router.get("/payments")
async def list_payments(
    principal: Principal = Depends(require_seller),
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status", max_length=24),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    _require_verify(principal)
    rows, total = await payments.list_requests(
        db, status=status_filter, limit=limit, offset=offset
    )
    grouped = (
        await db.execute(
            select(PaymentRequest.status, func.count(PaymentRequest.id)).group_by(
                PaymentRequest.status
            )
        )
    ).all()
    counts = {s.value: 0 for s in PaymentStatus}
    for state, count in grouped:
        counts[str(state)] = int(count)
    return {
        "total": total,
        "counts": counts,
        "requests": [payments.serialise(r) for r in rows],
    }


@router.get("/payments/{request_id}")
async def payment_detail(
    request_id: str,
    principal: Principal = Depends(require_seller),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_verify(principal)
    req = await payments.get_request(db, request_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment request not found.")
    data = payments.serialise(req)
    data["wallet_balance"] = await wallet_service.balance_of(db, req.user_id)
    return {"request": data}


@router.post("/payments/{request_id}/confirm")
async def confirm_payment(
    request_id: str,
    request: Request,
    principal: Principal = Depends(csrf_seller),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Sellers with the permission may confirm. The conditional state transition
    means a Master and a Seller clicking CONFIRM together still credit once (§19)."""
    _require_verify(principal)
    try:
        result = await payments.confirm(
            db, request_id=request_id, reviewer=principal, request=request
        )
    except payments.PaymentError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await db.commit()
    if result.already_processed:
        return {
            "ok": True,
            "already_processed": True,
            "message": f"This payment was already {result.request.status.lower()} — no coins were added again.",
            "request": payments.serialise(result.request),
        }
    return {
        "ok": True,
        "message": f"Payment confirmed. {result.coins_added:,} Coins added.",
        "coins_added": result.coins_added,
        "balance": result.balance,
        "request": payments.serialise(result.request),
    }


@router.post("/payments/{request_id}/reject")
async def reject_payment(
    request_id: str,
    payload: PaymentReviewIn,
    request: Request,
    principal: Principal = Depends(csrf_seller),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_verify(principal)
    try:
        result = await payments.reject(
            db, request_id=request_id, reviewer=principal, reason=payload.reason or "",
            request=request,
        )
    except payments.PaymentError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await db.commit()
    return {
        "ok": True,
        "already_processed": result.already_processed,
        "message": "Payment rejected. No coins were added.",
        "request": payments.serialise(result.request),
    }
