"""Viewer wallet: balance, ledger history, coin packages, payment requests."""
from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import sessions as sess
from app.auth.deps import Principal, csrf_viewer, require_viewer
from app.auth.ratelimit import enforce
from app.config import settings
from app.database import get_db
from app.models.base import PaymentStatus
from app.payments import service as payments
from app.services import audit, settings_store, uploads
from app.wallet import service as wallet_service

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


@router.get("")
async def my_wallet(
    principal: Principal = Depends(require_viewer), db: AsyncSession = Depends(get_db)
) -> dict:
    wallet = await wallet_service.get_or_create_wallet(db, principal.user.id)
    await db.commit()
    pending, _ = await payments.list_requests(
        db, status=PaymentStatus.PENDING, user_id=principal.user.id, limit=10
    )
    return {
        "balance": int(wallet.balance),
        "lifetime_credited": int(wallet.lifetime_credited),
        "lifetime_spent": int(wallet.lifetime_spent),
        "is_frozen": wallet.is_frozen,
        "pending_requests": [payments.serialise(r, include_user=False) for r in pending],
    }


@router.get("/transactions")
async def transactions(
    principal: Principal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows = await wallet_service.history(db, user_id=principal.user.id, limit=limit, offset=offset)
    return {
        "transactions": [wallet_service.serialise(t) for t in rows],
        "limit": limit,
        "offset": offset,
    }


@router.get("/packages")
async def packages(db: AsyncSession = Depends(get_db)) -> dict:
    rows = await payments.active_packages(db)
    methods = await payments.active_methods(db)
    values = await settings_store.all_settings(db)
    return {
        "packages": [
            {
                "id": p.id,
                "name": p.name,
                "coins": int(p.coins),
                "bonus_coins": int(p.bonus_coins),
                "total_coins": p.total_coins,
                "price_bdt": float(p.price_bdt),
                "badge": p.badge,
            }
            for p in rows
        ],
        "methods": [
            {
                "id": m.id,
                "name": m.name,
                "account_number": m.account_number,
                "account_name": m.account_name,
                "account_type": m.account_type,
                "instructions": m.instructions,
                "logo_url": m.logo_url,
            }
            for m in methods
        ],
        "instructions": values.get("payment.instructions"),
        "screenshot_note": values.get("payment.min_screenshot_note"),
        "max_screenshot_mb": settings.max_screenshot_mb,
    }


@router.post("/payment-request", status_code=status.HTTP_201_CREATED)
async def create_payment_request(
    request: Request,
    package_id: str = Form(..., max_length=64),
    method_id: str | None = Form(default=None, max_length=64),
    sender_number: str | None = Form(default=None, max_length=80),
    transaction_ref: str | None = Form(default=None, max_length=120),
    note: str | None = Form(default=None, max_length=1000),
    screenshot: UploadFile = File(...),
    principal: Principal = Depends(csrf_viewer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Uploading proof NEVER credits coins — it only opens a PENDING request."""
    await enforce(request, "payment-request", settings.upload_rate_limit, extra=principal.user.id)

    stored = await uploads.save_screenshot(screenshot)
    try:
        req = await payments.create_request(
            db,
            user=principal.user,
            package_id=package_id,
            method_id=method_id,
            screenshot=stored,
            sender_number=sender_number,
            transaction_ref=transaction_ref,
            note=note,
            ip=sess.client_ip(request),
        )
        await audit.log(
            db, action="payment.request", actor=principal, request=request,
            target_type="payment_request", target_id=req.id,
            summary=f"{principal.user.label} submitted {req.request_code} (৳{float(req.amount_bdt):,.2f})",
        )
        await db.commit()
    except payments.PaymentError as exc:
        await db.rollback()
        uploads.delete(stored.stored_path)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception:
        await db.rollback()
        uploads.delete(stored.stored_path)
        raise

    fresh = await payments.get_request(db, req.id)
    return {
        "ok": True,
        "message": "Payment submitted. Status: PENDING VERIFICATION.",
        "request": payments.serialise(fresh or req, include_user=False),
    }


@router.get("/payment-requests")
async def my_payment_requests(
    principal: Principal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await payments.list_requests(
        db, user_id=principal.user.id, limit=limit, offset=offset
    )
    return {
        "total": total,
        "requests": [payments.serialise(r, include_user=False) for r in rows],
    }
