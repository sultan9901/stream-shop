"""Manual coin-payment verification (spec §14–§22, §46).

Screenshot upload NEVER credits coins. A payment request is created in
``PENDING`` and only a Master or an authorised Seller can move it to
``CONFIRMED``. The transition is a conditional UPDATE, so two reviewers clicking
CONFIRM at the same moment produce exactly one coin credit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.codes import next_code
from app.models.base import NotificationKind, PaymentStatus, TxnType, utcnow
from app.models.payment import PaymentMethod, PaymentRequest, PaymentScreenshot
from app.models.wallet import CoinPackage
from app.notifications import service as notify
from app.services import audit
from app.services.uploads import StoredFile
from app.wallet import service as wallet_service

log = logging.getLogger("stream.payments")


class PaymentError(Exception):
    pass


@dataclass(slots=True)
class ReviewResult:
    request: PaymentRequest
    balance: int | None = None
    coins_added: int = 0
    already_processed: bool = False


# --------------------------------------------------------------------------
# creation
# --------------------------------------------------------------------------
async def active_packages(db: AsyncSession) -> list[CoinPackage]:
    stmt = (
        select(CoinPackage)
        .where(CoinPackage.is_active.is_(True))
        .order_by(CoinPackage.display_order, CoinPackage.coins)
    )
    return list((await db.execute(stmt)).scalars())


async def active_methods(db: AsyncSession) -> list[PaymentMethod]:
    stmt = (
        select(PaymentMethod)
        .where(PaymentMethod.is_active.is_(True))
        .order_by(PaymentMethod.display_order, PaymentMethod.name)
    )
    return list((await db.execute(stmt)).scalars())


async def create_request(
    db: AsyncSession,
    *,
    user,
    package_id: str,
    method_id: str | None,
    screenshot: StoredFile,
    sender_number: str | None,
    transaction_ref: str | None,
    note: str | None,
    ip: str | None,
) -> PaymentRequest:
    package = (
        await db.execute(
            select(CoinPackage).where(CoinPackage.id == package_id, CoinPackage.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if package is None:
        raise PaymentError("That coin package is no longer available.")

    method: PaymentMethod | None = None
    if method_id:
        method = (
            await db.execute(
                select(PaymentMethod).where(
                    PaymentMethod.id == method_id, PaymentMethod.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
    if method is None:
        method = next(iter(await active_methods(db)), None)
    if method is None:
        raise PaymentError("No payment method is configured. Please contact support.")

    req = PaymentRequest(
        request_code=await next_code(db, "payment"),
        user_id=user.id,
        package_id=package.id,
        payment_method_id=method.id,
        package_name=package.name,
        coins=int(package.coins),
        bonus_coins=int(package.bonus_coins),
        amount_bdt=float(package.price_bdt),
        method_name=method.name,
        method_number=method.account_number,
        sender_number=(sender_number or "").strip()[:80] or None,
        transaction_ref=(transaction_ref or "").strip()[:120] or None,
        note=(note or "").strip()[:1000] or None,
        status=PaymentStatus.PENDING,
        ip=ip,
    )
    db.add(req)
    await db.flush()

    db.add(
        PaymentScreenshot(
            request_id=req.id,
            original_name=screenshot.original_name,
            stored_path=screenshot.stored_path,
            content_type=screenshot.content_type,
            size_bytes=screenshot.size_bytes,
            width=screenshot.width,
            height=screenshot.height,
            checksum_sha256=screenshot.checksum_sha256,
        )
    )
    await db.flush()

    body = (
        f"Customer: {user.label}\nPackage: {req.package_name} ({req.total_coins:,} coins)\n"
        f"Amount: ৳{float(req.amount_bdt):,.2f}\nPayment Method: {req.method_name}"
    )
    await notify.push(
        db,
        audience="MASTER",
        kind=NotificationKind.NEW_COIN_PAYMENT,
        title="🔔 NEW COIN PAYMENT",
        body=body,
        icon="coin",
        link=f"/master#payments/{req.id}",
        payload={"request_id": req.id, "code": req.request_code},
    )
    await notify.push(
        db,
        audience="SELLER",
        kind=NotificationKind.NEW_COIN_PAYMENT,
        title="🔔 NEW COIN PAYMENT",
        body=body,
        icon="coin",
        link=f"/seller#payments/{req.id}",
        payload={"request_id": req.id, "code": req.request_code},
    )
    await notify.push(
        db,
        user_id=user.id,
        kind=NotificationKind.SYSTEM,
        title="Payment submitted — pending verification",
        body=(
            f"{notify.BRAND}\n\nWe received your payment proof for {req.package_name}.\n"
            f"Reference: {req.request_code}\n\nStatus: PENDING VERIFICATION"
        ),
        icon="clock",
        link="/wallet",
        payload={"request_id": req.id, "status": req.status},
    )
    return req


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------
async def _transition(db: AsyncSession, request_id: str, new_status: str, values: dict) -> bool:
    """Atomic PENDING -> ``new_status``. Returns False if someone got there first."""
    res = await db.execute(
        update(PaymentRequest)
        .where(PaymentRequest.id == request_id, PaymentRequest.status == PaymentStatus.PENDING)
        .values(status=new_status, reviewed_at=utcnow(), **values)
    )
    return bool(res.rowcount)


async def get_request(db: AsyncSession, request_id: str) -> PaymentRequest | None:
    stmt = (
        select(PaymentRequest)
        .where(PaymentRequest.id == request_id)
        .options(
            selectinload(PaymentRequest.screenshots),
            selectinload(PaymentRequest.user),
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def confirm(db: AsyncSession, *, request_id: str, reviewer, request=None) -> ReviewResult:
    req = await get_request(db, request_id)
    if req is None:
        raise PaymentError("Payment request not found.")
    if req.status != PaymentStatus.PENDING:
        return ReviewResult(request=req, already_processed=True)

    moved = await _transition(
        db,
        request_id,
        PaymentStatus.CONFIRMED,
        {"reviewed_by_id": reviewer.user.id, "reviewed_by_label": reviewer.user.label},
    )
    if not moved:
        db.expire(req)
        req = await get_request(db, request_id)
        return ReviewResult(request=req, already_processed=True)

    total = req.total_coins
    movement = await wallet_service.credit(
        db,
        user_id=req.user_id,
        coins=total,
        txn_type=TxnType.COIN_PURCHASE,
        idempotency_key=f"payreq:{req.id}:credit",
        reference_type="payment_request",
        reference_id=req.id,
        bdt_amount=float(req.amount_bdt),
        payment_method=req.method_name,
        reason=f"Coin package: {req.package_name}",
        performed_by_id=reviewer.user.id,
        performed_by_label=reviewer.user.label,
    )

    await db.execute(
        update(PaymentRequest)
        .where(PaymentRequest.id == req.id)
        .values(credited_txn_id=movement.transaction.id)
    )

    await notify.push(
        db,
        user_id=req.user_id,
        kind=NotificationKind.PAYMENT_CONFIRMED,
        title="✅ Payment confirmed",
        body=notify.coins_added_message(total, movement.balance),
        icon="check",
        link="/wallet",
        payload={
            "request_id": req.id,
            "coins": total,
            "balance": movement.balance,
            "reference": movement.transaction.reference_code,
        },
    )
    await notify.broadcast_wallet(db, req.user_id, movement.balance)
    await audit.log(
        db,
        action="payment.confirm",
        actor=reviewer,
        request=request,
        target_type="payment_request",
        target_id=req.id,
        summary=f"Confirmed {req.request_code} (+{total} coins) for {req.user.label if req.user else req.user_id}",
        meta={"coins": total, "bdt": float(req.amount_bdt), "txn": movement.transaction.reference_code},
    )
    db.expire(req)
    fresh = await get_request(db, request_id)
    return ReviewResult(request=fresh or req, balance=movement.balance, coins_added=total)


async def reject(
    db: AsyncSession, *, request_id: str, reviewer, reason: str, request=None
) -> ReviewResult:
    req = await get_request(db, request_id)
    if req is None:
        raise PaymentError("Payment request not found.")
    if req.status != PaymentStatus.PENDING:
        return ReviewResult(request=req, already_processed=True)

    reason = (reason or "").strip() or "Payment could not be verified."
    moved = await _transition(
        db,
        request_id,
        PaymentStatus.REJECTED,
        {
            "reviewed_by_id": reviewer.user.id,
            "reviewed_by_label": reviewer.user.label,
            "reject_reason": reason[:1000],
        },
    )
    if not moved:
        db.expire(req)
        return ReviewResult(request=await get_request(db, request_id) or req, already_processed=True)

    await notify.push(
        db,
        user_id=req.user_id,
        kind=NotificationKind.PAYMENT_REJECTED,
        title="⚠️ Payment rejected",
        body=(
            f"{notify.BRAND}\n\nYour payment for {req.package_name} could not be verified.\n\n"
            f"Reason: {reason}\n\nNo coins were added. Please contact support."
        ),
        icon="alert",
        link="/wallet",
        payload={"request_id": req.id, "reason": reason},
    )
    await audit.log(
        db,
        action="payment.reject",
        actor=reviewer,
        request=request,
        target_type="payment_request",
        target_id=req.id,
        summary=f"Rejected {req.request_code}: {reason}",
    )
    db.expire(req)
    return ReviewResult(request=await get_request(db, request_id) or req, coins_added=0)


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------
async def list_requests(
    db: AsyncSession,
    *,
    status: str | None = None,
    user_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[PaymentRequest], int]:
    stmt = select(PaymentRequest).options(
        selectinload(PaymentRequest.screenshots), selectinload(PaymentRequest.user)
    )
    count_stmt = select(func.count(PaymentRequest.id))
    if status:
        stmt = stmt.where(PaymentRequest.status == status)
        count_stmt = count_stmt.where(PaymentRequest.status == status)
    if user_id:
        stmt = stmt.where(PaymentRequest.user_id == user_id)
        count_stmt = count_stmt.where(PaymentRequest.user_id == user_id)
    total = int((await db.execute(count_stmt)).scalar() or 0)
    stmt = stmt.order_by(PaymentRequest.created_at.desc()).limit(min(limit, 200)).offset(max(offset, 0))
    return list((await db.execute(stmt)).scalars()), total


def serialise(req: PaymentRequest, *, include_user: bool = True) -> dict:
    data = {
        "id": req.id,
        "code": req.request_code,
        "package_name": req.package_name,
        "coins": int(req.coins),
        "bonus_coins": int(req.bonus_coins),
        "total_coins": req.total_coins,
        "amount_bdt": float(req.amount_bdt),
        "method_name": req.method_name,
        "method_number": req.method_number,
        "sender_number": req.sender_number,
        "transaction_ref": req.transaction_ref,
        "note": req.note,
        "status": req.status,
        "reviewed_by": req.reviewed_by_label,
        "reviewed_at": req.reviewed_at.isoformat() if req.reviewed_at else None,
        "reject_reason": req.reject_reason,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "screenshots": [
            {"id": s.id, "url": f"/api/payments/screenshot/{s.id}", "name": s.original_name}
            for s in (req.screenshots or [])
        ],
    }
    if include_user and req.user is not None:
        data["customer"] = {
            "id": req.user.id,
            "label": req.user.label,
            "email": req.user.email,
            "code": req.user.public_code,
        }
    return data
