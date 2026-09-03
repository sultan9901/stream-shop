"""Dashboard statistics and chart series."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import OrderStatus, PaymentStatus, Role, TxnType, utcnow
from app.models.order import Order
from app.models.payment import PaymentRequest
from app.models.product import Product
from app.models.user import User
from app.models.wallet import WalletTransaction


async def _count(db: AsyncSession, model, *conds) -> int:
    stmt = select(func.count(model.id))
    for c in conds:
        stmt = stmt.where(c)
    return int((await db.execute(stmt)).scalar() or 0)


async def _sum(db: AsyncSession, column, *conds) -> float:
    stmt = select(func.coalesce(func.sum(column), 0))
    for c in conds:
        stmt = stmt.where(c)
    return float((await db.execute(stmt)).scalar() or 0)


async def master_overview(db: AsyncSession) -> dict:
    coins_sold = await _sum(
        db, WalletTransaction.amount, WalletTransaction.txn_type == TxnType.COIN_PURCHASE
    )
    coins_spent = await _sum(
        db, WalletTransaction.amount, WalletTransaction.txn_type == TxnType.COIN_SPENT
    )
    bdt_total = await _sum(
        db,
        cast(PaymentRequest.amount_bdt, Numeric(14, 2)),
        PaymentRequest.status == PaymentStatus.CONFIRMED,
    )
    refunds = await _sum(
        db, WalletTransaction.amount, WalletTransaction.txn_type == TxnType.COIN_REFUND
    )
    return {
        "total_customers": await _count(db, User, User.role == Role.VIEWER),
        "total_sellers": await _count(db, User, User.role == Role.SELLER),
        "total_masters": await _count(db, User, User.role == Role.MASTER),
        "total_products": await _count(db, Product),
        "active_products": await _count(db, Product, Product.is_active.is_(True)),
        "total_orders": await _count(db, Order),
        "paid_orders": await _count(db, Order, Order.status == OrderStatus.PAID),
        "completed_orders": await _count(db, Order, Order.status == OrderStatus.COMPLETED),
        "refunded_orders": await _count(db, Order, Order.status == OrderStatus.REFUNDED),
        "pending_payments": await _count(
            db, PaymentRequest, PaymentRequest.status == PaymentStatus.PENDING
        ),
        "confirmed_payments": await _count(
            db, PaymentRequest, PaymentRequest.status == PaymentStatus.CONFIRMED
        ),
        "total_coins_sold": int(coins_sold),
        "total_coins_spent": int(abs(coins_spent)),
        "total_bdt_payments": round(bdt_total, 2),
        "total_refund_coins": int(refunds),
    }


async def seller_overview(db: AsyncSession, seller_id: str) -> dict:
    coins = await _sum(db, Order.coin_total, Order.seller_id == seller_id)
    return {
        "my_orders": await _count(db, Order, Order.seller_id == seller_id),
        "paid": await _count(db, Order, Order.seller_id == seller_id, Order.status == OrderStatus.PAID),
        "processing": await _count(
            db, Order, Order.seller_id == seller_id, Order.status == OrderStatus.PROCESSING
        ),
        "completed": await _count(
            db, Order, Order.seller_id == seller_id, Order.status == OrderStatus.COMPLETED
        ),
        "cancelled": await _count(
            db, Order, Order.seller_id == seller_id, Order.status == OrderStatus.CANCELLED
        ),
        "refunded": await _count(
            db, Order, Order.seller_id == seller_id, Order.status == OrderStatus.REFUNDED
        ),
        "my_products": await _count(db, Product, Product.seller_id == seller_id),
        "pending_payments": await _count(
            db, PaymentRequest, PaymentRequest.status == PaymentStatus.PENDING
        ),
        "coins_handled": int(coins),
    }


def _day_key(value) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]


async def daily_series(db: AsyncSession, days: int = 14) -> dict:
    """Per-day series for the dashboard charts."""
    since = utcnow() - timedelta(days=days - 1)
    labels = [(since + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    orders_rows = (
        await db.execute(
            select(Order.created_at, Order.coin_total).where(Order.created_at >= since)
        )
    ).all()
    pay_rows = (
        await db.execute(
            select(PaymentRequest.created_at, PaymentRequest.amount_bdt, PaymentRequest.status).where(
                PaymentRequest.created_at >= since
            )
        )
    ).all()
    coin_rows = (
        await db.execute(
            select(WalletTransaction.created_at, WalletTransaction.amount, WalletTransaction.txn_type)
            .where(WalletTransaction.created_at >= since)
        )
    ).all()

    orders = dict.fromkeys(labels, 0)
    coin_spend = dict.fromkeys(labels, 0)
    coin_buy = dict.fromkeys(labels, 0)
    revenue = dict.fromkeys(labels, 0.0)

    for created_at, coin_total in orders_rows:
        k = _day_key(created_at)
        if k in orders:
            orders[k] += 1
            coin_spend[k] += int(coin_total or 0)
    for created_at, amount, status in pay_rows:
        k = _day_key(created_at)
        if k in revenue and status == PaymentStatus.CONFIRMED:
            revenue[k] += float(amount or 0)
    for created_at, amount, txn_type in coin_rows:
        k = _day_key(created_at)
        if k in coin_buy and txn_type in (TxnType.COIN_PURCHASE, TxnType.BONUS_COIN):
            coin_buy[k] += int(amount or 0)

    return {
        "labels": labels,
        "orders": [orders[k] for k in labels],
        "coins_spent": [coin_spend[k] for k in labels],
        "coins_purchased": [coin_buy[k] for k in labels],
        "revenue_bdt": [round(revenue[k], 2) for k in labels],
    }


async def top_products(db: AsyncSession, limit: int = 6) -> list[dict]:
    stmt = (
        select(Product.id, Product.name, Product.sold_count, Product.coin_price)
        .order_by(Product.sold_count.desc())
        .limit(limit)
    )
    return [
        {"id": pid, "name": name, "sold": int(sold or 0), "coin_price": int(price or 0)}
        for pid, name, sold, price in (await db.execute(stmt)).all()
    ]
