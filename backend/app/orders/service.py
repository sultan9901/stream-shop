"""Coin-based purchase, order lifecycle and refunds (spec §23–§35, §60–§61).

Purchase transaction (all server side, one DB transaction):

    lock wallet -> verify balance -> duplicate guard -> deduct coins
    -> create order -> notify -> commit -> (background) deliver
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.codes import next_code
from app.delivery import tokens as download_tokens
from app.models.base import NotificationKind, OrderStatus, TxnType, utcnow
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.wallet import WalletTransaction
from app.notifications import service as notify
from app.services import audit
from app.wallet import service as wallet_service

log = logging.getLogger("stream.orders")

ACTIVE_STATUSES = (OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.COMPLETED)
REFUNDABLE = (OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.COMPLETED)


class OrderError(Exception):
    def __init__(self, message: str, code: str = "order_error") -> None:
        self.code = code
        super().__init__(message)


class AlreadyOwned(OrderError):
    def __init__(self, order_code: str) -> None:
        super().__init__(
            f"You already own this product (order {order_code}). Check your orders for the download.",
            code="already_owned",
        )
        self.order_code = order_code


async def existing_active_order(db: AsyncSession, user_id: str, product_id: str) -> Order | None:
    stmt = (
        select(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.user_id == user_id,
            OrderItem.product_id == product_id,
            Order.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def _order_by_debit_txn(db: AsyncSession, txn_id: str) -> Order | None:
    return (
        await db.execute(
            select(Order)
            .where(Order.debit_txn_id == txn_id)
            .options(selectinload(Order.items))
        )
    ).scalars().first()


async def purchase(
    db: AsyncSession,
    *,
    buyer: User,
    product_id: str,
    idempotency_key: str | None = None,
    ip: str | None = None,
    request=None,
) -> Order:
    product = (
        await db.execute(
            select(Product).where(Product.id == product_id).options(selectinload(Product.seller))
        )
    ).scalar_one_or_none()
    if product is None or not product.is_active:
        raise OrderError("This product is not available.", code="unavailable")
    if not product.in_stock:
        raise OrderError("This product is out of stock.", code="out_of_stock")
    price = int(product.coin_price)
    if price <= 0:
        raise OrderError("This product has no valid coin price yet.", code="bad_price")

    # 10-second dedup bucket collapses double-clicks even without a client key
    key = idempotency_key or f"purchase:{buyer.id}:{product.id}:{int(time.time() // 10)}"

    # Idempotent replay comes *first*: a retried or double-clicked request that
    # carries a key we have already spent coins under is the same logical
    # purchase, so hand back that order verbatim. The repurchase guard below is
    # only meant to reject a *distinct* new attempt to buy an owned product, and
    # would otherwise turn a harmless retry into a 409.
    existing_txn = await wallet_service.find_by_idempotency(db, key)
    if existing_txn is not None:
        replay = await _order_by_debit_txn(db, existing_txn.id)
        if replay is not None:
            return replay

    if not product.allow_repurchase:
        prior = await existing_active_order(db, buyer.id, product.id)
        if prior is not None:
            raise AlreadyOwned(prior.order_code)

    movement = await wallet_service.debit(
        db,
        user_id=buyer.id,
        coins=price,
        txn_type=TxnType.COIN_SPENT,
        idempotency_key=key,
        reference_type="product",
        reference_id=product.id,
        reason=f"Purchase: {product.name}",
        performed_by_id=buyer.id,
        performed_by_label=buyer.label,
    )

    if movement.duplicate:
        # A racer won between our replay probe and this debit. The coins were
        # spent exactly once, so we must attach to *their* order rather than mint
        # a second one against a single deduction.
        prior = await _order_by_debit_txn(db, movement.transaction.id)
        if prior is None:
            db.expire_all()
            prior = await _order_by_debit_txn(db, movement.transaction.id)
        if prior is None:
            prior = await existing_active_order(db, buyer.id, product.id)
        if prior is not None:
            return prior

    order = Order(
        order_code=await next_code(db, "order"),
        user_id=buyer.id,
        seller_id=product.seller_id,
        coin_total=price,
        status=OrderStatus.PAID,
        customer_email=buyer.email,
        customer_label=buyer.label,
        debit_txn_id=movement.transaction.id,
        paid_at=utcnow(),
        ip=ip,
    )
    db.add(order)
    await db.flush()
    db.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            product_name=product.name,
            product_version=product.version,
            coin_price=price,
            quantity=1,
        )
    )
    await db.execute(
        update(WalletTransaction)
        .where(WalletTransaction.id == movement.transaction.id)
        .values(reference_type="order", reference_id=order.id)
    )
    values = {"sold_count": Product.sold_count + 1}
    if product.stock is not None:
        # portable "never below zero" decrement (no GREATEST/MAX dialect split)
        values["stock"] = case((Product.stock > 0, Product.stock - 1), else_=0)
    await db.execute(update(Product).where(Product.id == product.id).values(**values))
    await db.flush()

    await _announce_new_order(db, order, product, movement.balance)
    await audit.log(
        db,
        action="order.purchase",
        actor=buyer,
        request=request,
        target_type="order",
        target_id=order.id,
        summary=f"{buyer.label} purchased {product.name} for {price} coins ({order.order_code})",
        meta={"coins": price, "balance_after": movement.balance, "product_id": product.id},
    )
    return order


def wallet_txn_table():  # pragma: no cover - retained for backwards compatibility
    return WalletTransaction.__table__


async def _announce_new_order(db: AsyncSession, order: Order, product: Product, balance: int) -> None:
    seller_label = product.seller.label if product.seller else "STREAM CORPORATION (house)"
    body = (
        f"Order:\n{order.order_code}\n\nProduct:\n{product.name}\n\n"
        f"Price:\n{int(order.coin_total):,} Coins\n\nCustomer:\n{order.customer_label}\n\n"
        f"Seller:\n{seller_label}\n\nStatus:\nPAID"
    )
    await notify.push(
        db,
        audience="MASTER",
        kind=NotificationKind.NEW_PRODUCT_ORDER,
        title="🔔 NEW PRODUCT ORDER",
        body=body,
        icon="cart",
        link=f"/master#orders/{order.id}",
        payload={
            "order_id": order.id,
            "order_code": order.order_code,
            "seller_id": order.seller_id,
            "seller_label": seller_label,
        },
    )
    if order.seller_id:
        await notify.push(
            db,
            user_id=order.seller_id,
            kind=NotificationKind.NEW_PRODUCT_ORDER,
            title="🔔 NEW PRODUCT ORDER",
            body=body,
            icon="cart",
            link=f"/seller#orders/{order.id}",
            payload={
                "order_id": order.id,
                "order_code": order.order_code,
                "seller_id": order.seller_id,
                "seller_label": seller_label,
            },
        )
    await notify.push(
        db,
        user_id=order.user_id,
        kind=NotificationKind.PRODUCT_PURCHASED,
        title="🛒 Purchase successful",
        body=(
            f"{notify.BRAND}\n\n{product.name} purchased for {int(order.coin_total):,} Coins.\n"
            f"Order ID: {order.order_code}\nRemaining balance: {balance:,} Coins\n\n"
            "Preparing your delivery..."
        ),
        icon="cart",
        link=f"/orders#{order.order_code}",
        payload={"order_id": order.id, "order_code": order.order_code, "balance": balance},
    )
    await notify.broadcast_wallet(db, order.user_id, balance)


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
async def get_order(db: AsyncSession, order_id: str) -> Order | None:
    stmt = (
        select(Order)
        .where(or_(Order.id == order_id, Order.order_code == order_id))
        .options(
            selectinload(Order.items), selectinload(Order.user), selectinload(Order.seller),
            selectinload(Order.deliveries),
        )
    )
    return (await db.execute(stmt)).scalars().first()


async def mark_completed(db: AsyncSession, *, order_id: str, actor, note: str | None = None,
                         request=None) -> Order:
    order = await get_order(db, order_id)
    if order is None:
        raise OrderError("Order not found.", code="not_found")
    if actor.user.is_seller and order.seller_id != actor.user.id:
        raise OrderError("This order is not assigned to you.", code="forbidden")
    if order.status == OrderStatus.COMPLETED:
        return order
    if order.status not in (OrderStatus.PAID, OrderStatus.PROCESSING):
        raise OrderError(f"An order in state {order.status} cannot be completed.", code="bad_state")

    res = await db.execute(
        update(Order)
        .where(Order.id == order.id, Order.status.in_([OrderStatus.PAID, OrderStatus.PROCESSING]))
        .values(
            status=OrderStatus.COMPLETED,
            completed_at=utcnow(),
            seller_note=(note or "").strip()[:1000] or order.seller_note,
        )
    )
    if not res.rowcount:
        db.expire(order)
        return await get_order(db, order_id) or order

    await notify.push(
        db,
        user_id=order.user_id,
        kind=NotificationKind.ORDER_COMPLETED,
        title="✅ Order completed",
        body=(
            f"{notify.BRAND}\n\nYour order {order.order_code} has been completed.\n"
            "Thank you for choosing STREAM CORPORATION."
        ),
        icon="check",
        link=f"/orders#{order.order_code}",
        payload={"order_id": order.id},
    )
    await audit.log(
        db, action="order.complete", actor=actor, request=request, target_type="order",
        target_id=order.id, summary=f"Completed {order.order_code}",
    )
    db.expire(order)
    return await get_order(db, order_id) or order


async def refund(
    db: AsyncSession, *, order_id: str, actor, reason: str, request=None, cancel: bool = False
) -> tuple[Order, int]:
    """Master-only. Returns ``(order, new_balance)``. Idempotent per order."""
    order = await get_order(db, order_id)
    if order is None:
        raise OrderError("Order not found.", code="not_found")
    if order.status == OrderStatus.REFUNDED:
        return order, await wallet_service.balance_of(db, order.user_id)
    if order.status not in REFUNDABLE:
        raise OrderError(f"An order in state {order.status} cannot be refunded.", code="bad_state")

    reason = (reason or "").strip() or "Refunded by Master"
    target = OrderStatus.CANCELLED if cancel else OrderStatus.REFUNDED
    res = await db.execute(
        update(Order)
        .where(Order.id == order.id, Order.status.in_(list(REFUNDABLE)))
        .values(
            status=target,
            refunded_at=utcnow(),
            cancelled_at=utcnow() if cancel else None,
            refund_reason=reason[:1000],
        )
    )
    if not res.rowcount:
        db.expire(order)
        fresh = await get_order(db, order_id) or order
        return fresh, await wallet_service.balance_of(db, fresh.user_id)

    movement = await wallet_service.credit(
        db,
        user_id=order.user_id,
        coins=int(order.coin_total),
        txn_type=TxnType.COIN_REFUND,
        idempotency_key=f"order:{order.id}:refund",
        reference_type="order",
        reference_id=order.id,
        reason=f"Refund for {order.order_code}: {reason}",
        performed_by_id=actor.user.id,
        performed_by_label=actor.user.label,
    )
    await db.execute(
        update(Order).where(Order.id == order.id).values(refund_txn_id=movement.transaction.id)
    )
    await download_tokens.revoke_for_order(db, order.id)

    await notify.push(
        db,
        user_id=order.user_id,
        kind=NotificationKind.ORDER_REFUNDED,
        title="↩️ Order refunded",
        body=(
            f"{notify.BRAND}\n\nOrder {order.order_code} has been refunded.\n\n"
            f"+{int(order.coin_total):,} Coins returned to your wallet.\n"
            f"Updated balance: {movement.balance:,} Coins\n\nReason: {reason}"
        ),
        icon="refund",
        link="/wallet",
        payload={"order_id": order.id, "coins": int(order.coin_total), "balance": movement.balance},
    )
    await notify.broadcast_wallet(db, order.user_id, movement.balance)
    await audit.log(
        db, action="order.refund" if not cancel else "order.cancel", actor=actor, request=request,
        target_type="order", target_id=order.id,
        summary=f"{target} {order.order_code} (+{int(order.coin_total)} coins) — {reason}",
        meta={"coins": int(order.coin_total), "balance_after": movement.balance},
    )
    db.expire(order)
    return (await get_order(db, order_id) or order), movement.balance


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------
async def list_orders(
    db: AsyncSession,
    *,
    user_id: str | None = None,
    seller_id: str | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Order], int]:
    stmt = select(Order).options(
        selectinload(Order.items), selectinload(Order.user), selectinload(Order.seller),
        selectinload(Order.deliveries),
    )
    count_stmt = select(func.count(Order.id))
    conds = []
    if user_id:
        conds.append(Order.user_id == user_id)
    if seller_id:
        conds.append(Order.seller_id == seller_id)
    if status:
        conds.append(Order.status == status)
    if search:
        like = f"%{search.strip()}%"
        conds.append(or_(Order.order_code.ilike(like), Order.customer_email.ilike(like),
                         Order.customer_label.ilike(like)))
    for c in conds:
        stmt = stmt.where(c)
        count_stmt = count_stmt.where(c)
    total = int((await db.execute(count_stmt)).scalar() or 0)
    stmt = stmt.order_by(Order.created_at.desc()).limit(min(limit, 200)).offset(max(offset, 0))
    return list((await db.execute(stmt)).scalars()), total


async def status_counts(db: AsyncSession, *, seller_id: str | None = None) -> dict[str, int]:
    stmt = select(Order.status, func.count(Order.id)).group_by(Order.status)
    if seller_id:
        stmt = stmt.where(Order.seller_id == seller_id)
    rows = (await db.execute(stmt)).all()
    counts = {s.value: 0 for s in OrderStatus}
    for status_value, count in rows:
        counts[str(status_value)] = int(count)
    return counts


def serialise(order: Order, *, include_customer: bool = True) -> dict:
    item = order.primary_item
    delivery = order.deliveries[-1] if order.deliveries else None
    data = {
        "id": order.id,
        "order_code": order.order_code,
        "status": order.status,
        "coin_total": int(order.coin_total),
        "product": {
            "id": item.product_id if item else None,
            "name": item.product_name if item else None,
            "version": item.product_version if item else None,
            "coin_price": int(item.coin_price) if item else None,
        },
        "seller": {"id": order.seller_id, "label": order.seller.label if order.seller else None},
        "delivery": {
            "status": delivery.status if delivery else None,
            "email_to": delivery.email_to if delivery else None,
            "sent_at": delivery.sent_at.isoformat() if delivery and delivery.sent_at else None,
            "error": delivery.last_error if delivery else None,
        },
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "refunded_at": order.refunded_at.isoformat() if order.refunded_at else None,
        "refund_reason": order.refund_reason,
        "seller_note": order.seller_note,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }
    if include_customer:
        data["customer"] = {
            "id": order.user_id,
            "label": order.customer_label,
            "email": order.customer_email,
            "code": order.user.public_code if order.user else None,
        }
    return data
