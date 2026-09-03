"""Product delivery: secure download grant + Gmail delivery + chat follow-up.

Idempotency: one ``deliveries`` row per order, guarded by a unique
``idempotency_key`` — the same order can never send two delivery emails, even if
the background task is retried or two workers race.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.delivery import tokens
from app.delivery.email import MAX_ATTACHMENT_BYTES, EmailError, send_email
from app.delivery.templates import render_delivery_email
from app.models.base import DeliveryStatus, NotificationKind, OrderStatus, utcnow
from app.models.order import Delivery, Order
from app.models.product import Product, ProductFile
from app.notifications import service as notify
from app.services import uploads

log = logging.getLogger("stream.delivery")

# The background delivery task competes with request handlers for SQLite's single
# writer. A lost race raises "database is locked"; these bound the retry.
_LOCK_RETRIES = 6
_LOCK_BACKOFF = 0.25  # seconds, multiplied by the attempt number


def _is_locked_error(exc: Exception) -> bool:
    """True for the transient SQLite "database is locked / busy" write race."""
    msg = str(getattr(exc, "orig", exc)).lower()
    return "database is locked" in msg or "database is busy" in msg


async def _get_order(db: AsyncSession, order_id: str) -> Order | None:
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.items), selectinload(Order.user))
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _claim_delivery(db: AsyncSession, order: Order) -> tuple[Delivery, bool]:
    """Return ``(delivery, is_new)``; only the creator may actually send."""
    key = f"order:{order.id}:email"
    existing = (
        await db.execute(select(Delivery).where(Delivery.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    row = Delivery(
        order_id=order.id,
        channel="EMAIL",
        email_to=order.customer_email,
        status=DeliveryStatus.QUEUED,
        idempotency_key=key,
    )
    db.add(row)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        db.expire_all()
        existing = (
            await db.execute(select(Delivery).where(Delivery.idempotency_key == key))
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False
    return row, True


async def deliver_order(db: AsyncSession, order_id: str, *, force: bool = False) -> dict:
    """Deliver the purchased software to the customer's Gmail address."""
    order = await _get_order(db, order_id)
    if order is None:
        return {"ok": False, "error": "order not found"}
    if order.status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
        return {"ok": False, "error": f"order is {order.status}"}

    delivery, is_new = await _claim_delivery(db, order)
    if not is_new and delivery.status == DeliveryStatus.SENT and not force:
        return {"ok": True, "duplicate": True, "delivery_id": delivery.id}

    item = order.primary_item
    product: Product | None = None
    if item and item.product_id:
        product = (
            await db.execute(
                select(Product)
                .where(Product.id == item.product_id)
                .options(selectinload(Product.files))
            )
        ).scalar_one_or_none()

    pfile: ProductFile | None = product.primary_file if product else None
    external = product.external_download_url if product else None

    token_row, raw_token = await tokens.issue(
        db,
        order_id=order.id,
        user_id=order.user_id,
        product_file_id=pfile.id if pfile else None,
        external_url=external,
    )
    link = tokens.download_url(raw_token)

    attachment = None
    if pfile and pfile.size_bytes and pfile.size_bytes <= MAX_ATTACHMENT_BYTES:
        try:
            blob = uploads.resolve(pfile.stored_path).read_bytes()
            attachment = (pfile.original_name, blob, pfile.content_type or "application/octet-stream")
        except Exception:
            log.warning("attachment unreadable for order %s; sending link only", order.order_code)

    subject, text, html = render_delivery_email(
        order=order,
        item=item,
        product=product,
        download_url=link,
        expires_at=token_row.expires_at,
        has_attachment=attachment is not None,
    )

    attempts = int(delivery.attempts) + 1
    try:
        transport = await send_email(
            to=order.customer_email or "",
            subject=subject,
            text=text,
            html=html,
            attachment=attachment,
        )
    except (EmailError, Exception) as exc:
        await db.execute(
            update(Delivery)
            .where(Delivery.id == delivery.id)
            .values(status=DeliveryStatus.FAILED, attempts=attempts, last_error=str(exc)[:1000])
        )
        await _notify_failure(db, order, str(exc))
        log.warning("delivery failed for %s: %s", order.order_code, exc)
        return {"ok": False, "error": str(exc), "delivery_id": delivery.id}

    await db.execute(
        update(Delivery)
        .where(Delivery.id == delivery.id)
        .values(
            status=DeliveryStatus.SENT,
            attempts=attempts,
            sent_at=utcnow(),
            last_error=None,
            email_to=order.customer_email,
        )
    )
    if order.status == OrderStatus.PAID:
        await db.execute(
            update(Order).where(Order.id == order.id).values(status=OrderStatus.PROCESSING)
        )

    await notify.push(
        db,
        user_id=order.user_id,
        kind=NotificationKind.PRODUCT_DELIVERED,
        title="📧 Product sent to your Gmail",
        body=notify.delivery_sent_message(order.order_code),
        icon="mail",
        link=f"/orders#{order.order_code}",
        payload={"order_id": order.id, "order_code": order.order_code, "download_url": link},
    )
    log.info("delivered %s via %s", order.order_code, transport)
    return {"ok": True, "delivery_id": delivery.id, "transport": transport, "download_url": link}


async def _notify_failure(db: AsyncSession, order: Order, error: str) -> None:
    await notify.push(
        db,
        user_id=order.user_id,
        kind=NotificationKind.DELIVERY_FAILED,
        title="⚠️ Email delivery failed",
        body=notify.delivery_failed_message(order.order_code),
        icon="alert",
        link=f"/orders#{order.order_code}",
        payload={"order_id": order.id},
    )
    body = (
        f"Order: {order.order_code}\nCustomer: {order.customer_label}\n"
        f"Email: {order.customer_email}\nError: {error[:300]}"
    )
    for audience in ("MASTER", "SELLER"):
        await notify.push(
            db,
            audience=audience,
            kind=NotificationKind.DELIVERY_FAILED,
            title="🔔 DELIVERY FAILED",
            body=body,
            icon="alert",
            link=f"/{audience.lower()}#orders/{order.id}",
            payload={"order_id": order.id, "order_code": order.order_code},
        )


async def delivery_status(db: AsyncSession, order_id: str) -> dict | None:
    row = (
        await db.execute(
            select(Delivery).where(Delivery.order_id == order_id).order_by(Delivery.created_at.desc())
        )
    ).scalars().first()
    if row is None:
        return None
    return {
        "id": row.id,
        "status": row.status,
        "attempts": int(row.attempts),
        "email_to": row.email_to,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "last_error": row.last_error,
    }


def backend_note() -> str:
    from app.delivery.email import backend_label

    return f"{backend_label()} · links expire in {settings.download_token_ttl_hours}h"


async def deliver_order_task(order_id: str, *, force: bool = False) -> dict:
    """Background-task entry point: owns its own DB session and commits itself.

    Never raises — a delivery failure is recorded on the ``deliveries`` row and
    surfaced as a notification, it must not crash the worker.

    SQLite serialises writers, and its ``busy_timeout`` deliberately does *not*
    cover an in-process collision: a session that began as a reader (the
    order/delivery/product SELECTs above) then promotes to a writer (the
    ``download_tokens`` INSERT) while another connection already holds the write
    lock. The session's writer lock (see ``app.database``) removes that race by
    serialising SQLite writers; the bounded retry below is a belt-and-braces
    guard for any lock error that still slips through.
    """
    from app.database import SessionLocal

    last_exc: Exception | None = None
    for attempt in range(_LOCK_RETRIES):
        try:
            async with SessionLocal() as db:
                result = await deliver_order(db, order_id, force=force)
                await db.commit()
                return result
        except OperationalError as exc:
            if not _is_locked_error(exc) or attempt == _LOCK_RETRIES - 1:
                log.exception("delivery task crashed for order %s", order_id)
                return {"ok": False, "error": str(exc)}
            last_exc = exc
            delay = _LOCK_BACKOFF * (attempt + 1)
            log.warning(
                "delivery for order %s hit a write-lock race (attempt %d/%d); retrying in %.2fs",
                order_id, attempt + 1, _LOCK_RETRIES, delay,
            )
            await asyncio.sleep(delay)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("delivery task crashed for order %s", order_id)
            return {"ok": False, "error": str(exc)}
    # Loop only exits via return above; this is unreachable but keeps types honest.
    return {"ok": False, "error": str(last_exc) if last_exc else "delivery failed"}
