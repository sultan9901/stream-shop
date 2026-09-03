"""Viewer order flow: coin purchase, order history, download re-issue."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import sessions as sess
from app.auth.deps import Principal, csrf_viewer, require_viewer
from app.auth.ratelimit import enforce
from app.config import settings
from app.delivery import service as delivery_service
from app.delivery import tokens as download_tokens
from app.database import get_db
from app.models.base import OrderStatus
from app.models.product import Product
from app.orders import service as orders
from app.schemas.commerce import PurchaseIn
from app.wallet import service as wallet_service

log = logging.getLogger("stream.routes.orders")
router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.post("/purchase", status_code=status.HTTP_201_CREATED)
async def purchase(
    payload: PurchaseIn,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(csrf_viewer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Buy a product with coins. Every check and the coin deduction happen here,
    server side, inside one database transaction."""
    await enforce(request, "purchase", settings.purchase_rate_limit, extra=principal.user.id)

    try:
        order = await orders.purchase(
            db,
            buyer=principal.user,
            product_id=payload.product_id,
            idempotency_key=(payload.idempotency_key or "").strip() or None,
            ip=sess.client_ip(request),
            request=request,
        )
        await db.commit()
    except wallet_service.InsufficientCoins as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            {
                "error": "insufficient_coins",
                "message": f"You need {exc.shortfall:,} more Coins.",
                "required": exc.required,
                "balance": exc.available,
                "shortfall": exc.shortfall,
            },
        ) from exc
    except wallet_service.WalletFrozen as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except orders.AlreadyOwned as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"error": exc.code, "message": str(exc), "order_code": exc.order_code},
        ) from exc
    except orders.OrderError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, {"error": exc.code, "message": str(exc)}
        ) from exc

    # email delivery runs after the response so a slow SMTP server never blocks
    # the purchase; it is idempotent per order.
    background.add_task(delivery_service.deliver_order_task, order.id)

    fresh = await orders.get_order(db, order.id)
    balance = await wallet_service.balance_of(db, principal.user.id)
    return {
        "ok": True,
        "message": "Purchase successful. Your product is being sent to your Gmail.",
        "balance": balance,
        "order": orders.serialise(fresh or order, include_customer=False),
    }


@router.get("")
async def my_orders(
    principal: Principal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status", max_length=24),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await orders.list_orders(
        db, user_id=principal.user.id, status=status_filter, limit=limit, offset=offset
    )
    return {
        "total": total,
        "counts": await _my_counts(db, principal.user.id),
        "orders": [orders.serialise(o, include_customer=False) for o in rows],
    }


async def _my_counts(db: AsyncSession, user_id: str) -> dict[str, int]:
    from sqlalchemy import func

    from app.models.order import Order

    rows = (
        await db.execute(
            select(Order.status, func.count(Order.id))
            .where(Order.user_id == user_id)
            .group_by(Order.status)
        )
    ).all()
    counts = {s.value: 0 for s in OrderStatus}
    for value, count in rows:
        counts[str(value)] = int(count)
    return counts


@router.get("/{order_id}")
async def order_detail(
    order_id: str,
    principal: Principal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    order = await orders.get_order(db, order_id)
    if order is None or order.user_id != principal.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    data = orders.serialise(order, include_customer=False)
    item = order.primary_item
    if item and item.product_id:
        product = (
            await db.execute(
                select(Product).where(Product.id == item.product_id).options(
                    selectinload(Product.files)
                )
            )
        ).scalar_one_or_none()
        if product is not None:
            data["product"].update(
                {
                    "slug": product.slug,
                    "thumbnail_url": product.thumbnail_url,
                    "delivery_note": product.delivery_note,
                }
            )
    return data


@router.post("/{order_id}/download-link")
async def reissue_download_link(
    order_id: str,
    request: Request,
    principal: Principal = Depends(csrf_viewer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Issue a fresh, expiring download grant for an order the caller owns."""
    await enforce(request, "download-link", "20/1h", extra=principal.user.id)
    order = await orders.get_order(db, order_id)
    if order is None or order.user_id != principal.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    if order.status in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This order was refunded, so downloads are disabled."
        )

    item = order.primary_item
    product = None
    if item and item.product_id:
        product = (
            await db.execute(
                select(Product).where(Product.id == item.product_id).options(
                    selectinload(Product.files)
                )
            )
        ).scalar_one_or_none()
    pfile = product.primary_file if product else None
    external = product.external_download_url if product else None
    if pfile is None and not external:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This product has no download attached yet. Support has been notified.",
        )

    token_row, raw = await download_tokens.issue(
        db,
        order_id=order.id,
        user_id=principal.user.id,
        product_file_id=pfile.id if pfile else None,
        external_url=external,
    )
    await db.commit()
    return {
        "ok": True,
        "download_url": download_tokens.download_url(raw),
        "expires_at": token_row.expires_at.isoformat(),
        "max_downloads": token_row.max_downloads,
    }


@router.post("/{order_id}/resend-email")
async def resend_delivery_email(
    order_id: str,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(csrf_viewer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await enforce(request, "resend-delivery", "5/1h", extra=principal.user.id)
    order = await orders.get_order(db, order_id)
    if order is None or order.user_id != principal.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
    if order.status in (OrderStatus.REFUNDED, OrderStatus.CANCELLED):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This order is {order.status}.")
    background.add_task(delivery_service.deliver_order_task, order.id, force=True)
    return {"ok": True, "message": "We are re-sending your product to your Gmail."}
