"""Master panel — coin packages & payment methods (spec §38–§39)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import Principal, csrf_master, require_master
from app.database import get_db
from app.models.payment import PaymentMethod, PaymentRequest
from app.models.wallet import CoinPackage
from app.schemas.admin import CoinPackageIn, PaymentMethodIn
from app.schemas.auth import MessageOut
from app.services import audit

router = APIRouter(prefix="/api/master", tags=["master-store"])


def serialise_package(p: CoinPackage) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "coins": int(p.coins),
        "bonus_coins": int(p.bonus_coins),
        "total_coins": int(p.coins) + int(p.bonus_coins),
        "price_bdt": float(p.price_bdt),
        "badge": p.badge,
        "is_active": p.is_active,
        "display_order": p.display_order,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def serialise_method(m: PaymentMethod) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "account_number": m.account_number,
        "account_name": m.account_name,
        "account_type": m.account_type,
        "instructions": m.instructions,
        "logo_url": m.logo_url,
        "is_active": m.is_active,
        "display_order": m.display_order,
    }


# ---------------------------------------------------------------- coin packages
@router.get("/coin-packages")
async def list_packages(
    _: Principal = Depends(require_master), db: AsyncSession = Depends(get_db)
) -> dict:
    rows = list(
        (
            await db.execute(
                select(CoinPackage).order_by(CoinPackage.display_order, CoinPackage.coins)
            )
        ).scalars()
    )
    return {"packages": [serialise_package(p) for p in rows]}


@router.post("/coin-packages", status_code=status.HTTP_201_CREATED)
async def create_package(
    payload: CoinPackageIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = CoinPackage(
        name=payload.name.strip(),
        coins=int(payload.coins),
        bonus_coins=int(payload.bonus_coins),
        price_bdt=round(float(payload.price_bdt), 2),
        badge=(payload.badge or "").strip() or None,
        is_active=bool(payload.is_active),
        display_order=int(payload.display_order or 0),
    )
    db.add(row)
    await db.flush()
    await audit.log(
        db, action="store.package_create", actor=principal, request=request,
        target_type="coin_package", target_id=row.id,
        summary=f"Created package {row.name} ({row.coins}+{row.bonus_coins} coins / ৳{row.price_bdt})",
    )
    await db.commit()
    return {"ok": True, "package": serialise_package(row)}


@router.put("/coin-packages/{package_id}")
async def update_package(
    package_id: str,
    payload: CoinPackageIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (
        await db.execute(select(CoinPackage).where(CoinPackage.id == package_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Coin package not found.")
    row.name = payload.name.strip()
    row.coins = int(payload.coins)
    row.bonus_coins = int(payload.bonus_coins)
    row.price_bdt = round(float(payload.price_bdt), 2)
    row.badge = (payload.badge or "").strip() or None
    row.is_active = bool(payload.is_active)
    row.display_order = int(payload.display_order or 0)
    await db.flush()
    await audit.log(
        db, action="store.package_update", actor=principal, request=request,
        target_type="coin_package", target_id=row.id, summary=f"Updated package {row.name}",
    )
    await db.commit()
    return {"ok": True, "package": serialise_package(row)}


@router.delete("/coin-packages/{package_id}", response_model=MessageOut)
async def delete_package(
    package_id: str,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    row = (
        await db.execute(select(CoinPackage).where(CoinPackage.id == package_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Coin package not found.")
    used = int(
        (
            await db.execute(
                select(func.count(PaymentRequest.id)).where(PaymentRequest.package_id == row.id)
            )
        ).scalar()
        or 0
    )
    name = row.name
    if used:
        # payment history references this package, so retire it instead of deleting
        row.is_active = False
        await db.flush()
        await audit.log(
            db, action="store.package_retire", actor=principal, request=request,
            target_type="coin_package", target_id=package_id, summary=f"Retired package {name}",
        )
        await db.commit()
        return MessageOut(
            message=f"{name} has payment history, so it was deactivated instead of deleted."
        )
    await db.delete(row)
    await audit.log(
        db, action="store.package_delete", actor=principal, request=request,
        target_type="coin_package", target_id=package_id, summary=f"Deleted package {name}",
    )
    await db.commit()
    return MessageOut(message=f"Package {name} deleted.")


# ---------------------------------------------------------------- payment methods
@router.get("/payment-methods")
async def list_methods(
    _: Principal = Depends(require_master), db: AsyncSession = Depends(get_db)
) -> dict:
    rows = list(
        (
            await db.execute(
                select(PaymentMethod).order_by(PaymentMethod.display_order, PaymentMethod.name)
            )
        ).scalars()
    )
    return {"methods": [serialise_method(m) for m in rows]}


@router.post("/payment-methods", status_code=status.HTTP_201_CREATED)
async def create_method(
    payload: PaymentMethodIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = PaymentMethod(
        name=payload.name.strip(),
        account_number=payload.account_number.strip(),
        account_name=(payload.account_name or "").strip() or None,
        account_type=(payload.account_type or "").strip() or None,
        instructions=payload.instructions or None,
        is_active=bool(payload.is_active),
        display_order=int(payload.display_order or 0),
    )
    db.add(row)
    await db.flush()
    await audit.log(
        db, action="store.method_create", actor=principal, request=request,
        target_type="payment_method", target_id=row.id,
        summary=f"Added payment method {row.name} ({row.account_number})",
    )
    await db.commit()
    return {"ok": True, "method": serialise_method(row)}


@router.put("/payment-methods/{method_id}")
async def update_method(
    method_id: str,
    payload: PaymentMethodIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (
        await db.execute(select(PaymentMethod).where(PaymentMethod.id == method_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment method not found.")
    before = {"number": row.account_number, "active": row.is_active}
    row.name = payload.name.strip()
    row.account_number = payload.account_number.strip()
    row.account_name = (payload.account_name or "").strip() or None
    row.account_type = (payload.account_type or "").strip() or None
    row.instructions = payload.instructions or None
    row.is_active = bool(payload.is_active)
    row.display_order = int(payload.display_order or 0)
    await db.flush()
    await audit.log(
        db, action="store.method_update", actor=principal, request=request,
        target_type="payment_method", target_id=row.id,
        summary=f"Updated payment method {row.name}",
        meta={"before": before, "after": {"number": row.account_number, "active": row.is_active}},
    )
    await db.commit()
    return {"ok": True, "method": serialise_method(row)}


@router.delete("/payment-methods/{method_id}", response_model=MessageOut)
async def delete_method(
    method_id: str,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    row = (
        await db.execute(select(PaymentMethod).where(PaymentMethod.id == method_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment method not found.")
    used = int(
        (
            await db.execute(
                select(func.count(PaymentRequest.id)).where(
                    PaymentRequest.payment_method_id == row.id
                )
            )
        ).scalar()
        or 0
    )
    name = row.name
    if used:
        row.is_active = False
        await db.flush()
        await audit.log(
            db, action="store.method_retire", actor=principal, request=request,
            target_type="payment_method", target_id=method_id,
            summary=f"Deactivated payment method {name}",
        )
        await db.commit()
        return MessageOut(
            message=f"{name} was used by {used} payment(s), so it was deactivated instead of deleted."
        )
    await db.delete(row)
    await audit.log(
        db, action="store.method_delete", actor=principal, request=request,
        target_type="payment_method", target_id=method_id,
        summary=f"Deleted payment method {name}",
    )
    await db.commit()
    return MessageOut(message=f"Payment method {name} deleted.")
