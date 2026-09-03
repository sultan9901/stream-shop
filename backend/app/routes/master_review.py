"""Master panel — payment verification, order control, wallet control (§18–§22, §33–§34, §41)."""
from __future__ import annotations

import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import Principal, csrf_master, require_master
from app.database import get_db
from app.delivery import service as delivery_service
from app.models.base import NotificationKind, OrderStatus, PaymentStatus, Role, TxnType
from app.models.payment import PaymentRequest
from app.models.user import User
from app.notifications import service as notify
from app.orders import service as orders
from app.payments import service as payments
from app.schemas.admin import WalletAdjustIn
from app.schemas.auth import MessageOut
from app.schemas.commerce import OrderNoteIn, PaymentReviewIn, RefundIn
from app.services import audit
from app.wallet import service as wallet_service

router = APIRouter(prefix="/api/master", tags=["master-review"])


async def load_viewer(db: AsyncSession, ident: str) -> User | None:
    """Find a customer by id, public code or Gmail address."""
    ident = (ident or "").strip()
    stmt = select(User).where(
        User.role == Role.VIEWER,
        or_(User.id == ident, User.public_code == ident, func.lower(User.email) == ident.lower()),
    )
    return (await db.execute(stmt)).scalars().first()


# ---------------------------------------------------------------- payments
@router.get("/payments")
async def list_payments(
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status", max_length=24),
    user_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await payments.list_requests(
        db, status=status_filter, user_id=user_id, limit=limit, offset=offset
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
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
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
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Credits coins exactly once — the state transition is a conditional UPDATE."""
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
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Rejection never credits coins (§46)."""
    try:
        result = await payments.reject(
            db, request_id=request_id, reviewer=principal, reason=payload.reason or "",
            request=request,
        )
    except payments.PaymentError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await db.commit()
    if result.already_processed:
        return {
            "ok": True,
            "already_processed": True,
            "message": f"This payment was already {result.request.status.lower()}.",
            "request": payments.serialise(result.request),
        }
    return {
        "ok": True,
        "message": "Payment rejected. No coins were added.",
        "request": payments.serialise(result.request),
    }


# ---------------------------------------------------------------- orders
@router.get("/orders")
async def list_orders(
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status", max_length=24),
    q: str | None = Query(default=None, max_length=120),
    seller_id: str | None = Query(default=None, max_length=64),
    user_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await orders.list_orders(
        db, user_id=user_id, seller_id=seller_id, status=status_filter, search=q,
        limit=limit, offset=offset,
    )
    counts = await orders.status_counts(db)
    return {
        "total": total,
        "counts": {"total": sum(counts.values()), **counts},
        "orders": [orders.serialise(o) for o in rows],
    }


@router.get("/orders/{order_id}")
async def order_detail(
    order_id: str,
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    order = await orders.get_order(db, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    data = orders.serialise(order)
    data["wallet_balance"] = await wallet_service.balance_of(db, order.user_id)
    return {"order": data}


@router.post("/orders/{order_id}/complete")
async def complete_order(
    order_id: str,
    payload: OrderNoteIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        order = await orders.mark_completed(
            db, order_id=order_id, actor=principal, note=payload.note, request=request
        )
    except orders.OrderError as exc:
        await db.rollback()
        code = (
            status.HTTP_404_NOT_FOUND if exc.code == "not_found" else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(code, {"error": exc.code, "message": str(exc)}) from exc
    await db.commit()
    return {"ok": True, "message": f"{order.order_code} marked completed.",
            "order": orders.serialise(order)}


@router.post("/orders/{order_id}/refund")
async def refund_order(
    order_id: str,
    payload: RefundIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Master-only refund: coins go back to the wallet ledger and download grants
    for the order are revoked. Idempotent — a second call adds nothing (§34)."""
    try:
        order, balance = await orders.refund(
            db, order_id=order_id, actor=principal, reason=payload.reason,
            request=request, cancel=payload.cancel,
        )
    except orders.OrderError as exc:
        await db.rollback()
        code = (
            status.HTTP_404_NOT_FOUND if exc.code == "not_found" else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(code, {"error": exc.code, "message": str(exc)}) from exc
    await db.commit()
    return {
        "ok": True,
        "message": (
            f"{order.order_code} is now {order.status}. "
            f"{int(order.coin_total):,} Coins were returned to the customer."
        ),
        "balance": balance,
        "order": orders.serialise(order),
    }


@router.post("/orders/{order_id}/redeliver")
async def redeliver_order(
    order_id: str,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    order = await orders.get_order(db, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    if order.status in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"This order is {order.status} — delivery is disabled."
        )
    await audit.log(
        db, action="order.redeliver", actor=principal, request=request, target_type="order",
        target_id=order.id, summary=f"Re-sent delivery email for {order.order_code}",
    )
    await db.commit()
    background.add_task(delivery_service.deliver_order_task, order.id, force=True)
    return MessageOut(message=f"Delivery email for {order.order_code} is being re-sent.")


# ---------------------------------------------------------------- wallet control
@router.post("/customers/{user_id}/wallet")
async def adjust_wallet(
    user_id: str,
    payload: WalletAdjustIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manual Add / Remove / Bonus coins. A reason is mandatory and every movement
    is a ledger transaction plus an audit-log entry (§41, §49)."""
    user = await load_viewer(db, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found.")

    reason = payload.reason.strip()
    coins = int(payload.coins)
    # unique per master action, so a double-click cannot double-adjust
    key = f"admin:{principal.user.id}:{user.id}:{payload.direction}:{coins}:{int(time.time() // 10)}"
    try:
        if payload.direction == "remove":
            movement = await wallet_service.debit(
                db, user_id=user.id, coins=coins, txn_type=TxnType.ADMIN_DEBIT,
                idempotency_key=key, reason=reason, reference_type="admin",
                reference_id=principal.user.id,
                performed_by_id=principal.user.id, performed_by_label=principal.user.label,
            )
        else:
            movement = await wallet_service.credit(
                db, user_id=user.id, coins=coins,
                txn_type=TxnType.BONUS_COIN if payload.direction == "bonus" else TxnType.ADMIN_CREDIT,
                idempotency_key=key, reason=reason, reference_type="admin",
                reference_id=principal.user.id,
                performed_by_id=principal.user.id, performed_by_label=principal.user.label,
            )
    except wallet_service.InsufficientCoins as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {
                "error": "insufficient_coins",
                "message": f"That wallet only holds {exc.available:,} Coins.",
                "balance": exc.available,
                "required": exc.required,
            },
        ) from exc
    except wallet_service.WalletFrozen as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    verb = {"add": "added to", "bonus": "granted to", "remove": "removed from"}[payload.direction]
    sign = "-" if payload.direction == "remove" else "+"
    await notify.push(
        db,
        user_id=user.id,
        kind=NotificationKind.COINS_ADDED if payload.direction != "remove" else NotificationKind.SYSTEM,
        title="💰 Wallet updated" if payload.direction != "remove" else "⚠️ Wallet adjusted",
        body=(
            f"{notify.BRAND}\n\n{sign}{coins:,} Coins {verb} your wallet by an administrator.\n"
            f"Updated balance: {movement.balance:,} Coins\n\nReason: {reason}"
        ),
        icon="coin",
        link="/wallet",
        payload={"coins": coins, "direction": payload.direction, "balance": movement.balance},
    )
    await notify.broadcast_wallet(db, user.id, movement.balance)
    await audit.log(
        db, action=f"wallet.{payload.direction}", actor=principal, request=request,
        target_type="user", target_id=user.id,
        summary=f"{payload.direction} {coins} coins for {user.label}: {reason}",
        meta={
            "coins": coins,
            "balance_after": movement.balance,
            "txn": movement.transaction.reference_code,
            "duplicate": movement.duplicate,
        },
    )
    await db.commit()
    return {
        "ok": True,
        "message": f"{sign}{coins:,} Coins {verb} {user.label}. New balance: {movement.balance:,}.",
        "balance": movement.balance,
        "transaction": wallet_service.serialise(movement.transaction),
    }
