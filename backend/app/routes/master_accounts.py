"""Master panel — Master & Seller account administration (spec §4–§9)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import device as device_mod
from app.auth import sessions as sess
from app.auth.deps import Principal, csrf_master, require_master
from app.auth.security import hash_password
from app.database import get_db
from app.models.base import OrderStatus, Role
from app.models.order import Order
from app.models.user import User
from app.schemas.admin import AccountUpdateIn, MasterCreateIn, ResetPasswordIn, SellerCreateIn
from app.schemas.auth import MessageOut
from app.services import accounts, audit

router = APIRouter(prefix="/api/master", tags=["master-accounts"])


def _serialise_device(d) -> dict:
    return {
        "id": d.id,
        "label": d.label,
        "platform": d.platform,
        "is_active": d.is_active,
        "first_ip": d.first_ip,
        "last_ip": d.last_ip,
        "bound_at": d.bound_at.isoformat() if d.bound_at else None,
        "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
    }


async def _staff_payload(db: AsyncSession, user: User) -> dict:
    data = accounts.serialise_user(user)
    devices = await device_mod.list_devices(db, user.id)
    data["devices"] = [_serialise_device(d) for d in devices]
    data["bound_device"] = next((d.label for d in devices if d.is_active), None)
    if user.role == Role.MASTER and user.master_account:
        data["is_root"] = user.master_account.is_root
        data["note"] = user.master_account.note
    if user.role == Role.SELLER and user.seller_account:
        data["seller_code"] = user.seller_account.seller_code
        data["contact_email"] = user.seller_account.contact_email
        data["can_verify_payments"] = user.seller_account.can_verify_payments
        data["note"] = user.seller_account.note
        stats = (
            await db.execute(
                select(Order.status, func.count(Order.id))
                .where(Order.seller_id == user.id)
                .group_by(Order.status)
            )
        ).all()
        counts = {s.value: 0 for s in OrderStatus}
        for value, count in stats:
            counts[str(value)] = int(count)
        data["order_stats"] = {"total": sum(counts.values()), **counts}
    return data


async def _load_staff(db: AsyncSession, user_id: str, roles: tuple[str, ...]) -> User:
    user = (
        await db.execute(
            select(User)
            .where(User.id == user_id, User.role.in_(list(roles)))
            .options(selectinload(User.master_account), selectinload(User.seller_account))
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found.")
    return user


async def _list_role(db: AsyncSession, role: str, q: str | None) -> list[dict]:
    stmt = (
        select(User)
        .where(User.role == role)
        .options(selectinload(User.master_account), selectinload(User.seller_account))
        .order_by(User.created_at.asc())
    )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.public_code.ilike(like)))
    rows = list((await db.execute(stmt)).scalars())
    return [await _staff_payload(db, u) for u in rows]


@router.get("/masters")
async def list_masters(
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None, max_length=80),
) -> dict:
    return {"masters": await _list_role(db, Role.MASTER, q)}


@router.get("/sellers")
async def list_sellers(
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None, max_length=80),
) -> dict:
    return {"sellers": await _list_role(db, Role.SELLER, q)}


@router.post("/masters", status_code=status.HTTP_201_CREATED)
async def create_master(
    payload: MasterCreateIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        user = await accounts.create_master(
            db,
            username=payload.username,
            password=payload.password,
            created_by=principal.user,
            device_lock=payload.device_lock,
            note=payload.note,
            must_change_password=True,
        )
    except accounts.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await audit.log(
        db, action="account.create_master", actor=principal, request=request, target_type="user",
        target_id=user.id, summary=f"Created master {user.username}",
    )
    await db.commit()
    fresh = await _load_staff(db, user.id, (Role.MASTER,))
    return {"ok": True, "master": await _staff_payload(db, fresh)}


@router.post("/sellers", status_code=status.HTTP_201_CREATED)
async def create_seller(
    payload: SellerCreateIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        user = await accounts.create_seller(
            db,
            username=payload.username,
            password=payload.password,
            created_by=principal.user,
            contact_email=payload.contact_email,
            device_lock=payload.device_lock,
            can_verify_payments=payload.can_verify_payments,
            note=payload.note,
        )
    except accounts.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await audit.log(
        db, action="account.create_seller", actor=principal, request=request, target_type="user",
        target_id=user.id, summary=f"Created seller {user.username}",
    )
    await db.commit()
    fresh = await _load_staff(db, user.id, (Role.SELLER,))
    return {"ok": True, "seller": await _staff_payload(db, fresh)}


def _is_root(user: User) -> bool:
    return bool(user.role == Role.MASTER and user.master_account and user.master_account.is_root)


@router.patch("/accounts/{user_id}")
async def update_account(
    user_id: str,
    payload: AccountUpdateIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Enable/disable, toggle device lock, edit notes and seller permissions."""
    user = await _load_staff(db, user_id, (Role.MASTER, Role.SELLER, Role.VIEWER))
    changes: dict = {}

    if payload.is_active is not None and payload.is_active != user.is_active:
        if not payload.is_active and _is_root(user):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The root master cannot be disabled.")
        if not payload.is_active and user.id == principal.user.id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "You cannot disable your own account."
            )
        user.is_active = payload.is_active
        changes["is_active"] = payload.is_active
        if not payload.is_active:
            # a disabled account must lose every live session immediately
            await sess.revoke_all_for_user(db, user.id)

    if payload.device_lock is not None and payload.device_lock != user.device_lock_enabled:
        user.device_lock_enabled = payload.device_lock
        changes["device_lock"] = payload.device_lock

    if user.role == Role.MASTER and user.master_account is not None:
        if payload.note is not None:
            user.master_account.note = payload.note or None
            changes["note"] = True
    if user.role == Role.SELLER and user.seller_account is not None:
        if payload.note is not None:
            user.seller_account.note = payload.note or None
            changes["note"] = True
        if payload.contact_email is not None:
            user.seller_account.contact_email = payload.contact_email.strip() or None
            changes["contact_email"] = user.seller_account.contact_email
        if payload.can_verify_payments is not None:
            user.seller_account.can_verify_payments = payload.can_verify_payments
            changes["can_verify_payments"] = payload.can_verify_payments

    await db.flush()
    await audit.log(
        db, action="account.update", actor=principal, request=request, target_type="user",
        target_id=user.id, summary=f"Updated account {user.label}", meta=changes,
    )
    await db.commit()
    fresh = await _load_staff(db, user.id, (Role.MASTER, Role.SELLER, Role.VIEWER))
    return {"ok": True, "account": await _staff_payload(db, fresh)}


@router.post("/accounts/{user_id}/reset-password", response_model=MessageOut)
async def reset_password(
    user_id: str,
    payload: ResetPasswordIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    user = await _load_staff(db, user_id, (Role.MASTER, Role.SELLER))
    try:
        new_password = accounts.validate_password(payload.new_password)
    except accounts.AccountError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    user.password_hash = hash_password(new_password)
    user.must_change_password = payload.force_change
    # every existing staff session for that account is invalidated
    await sess.revoke_all_for_user(db, user.id, sess.STAFF)
    await audit.log(
        db, action="account.reset_password", actor=principal, request=request, target_type="user",
        target_id=user.id, summary=f"Reset password for {user.label}",
    )
    await db.commit()
    return MessageOut(message=f"Password reset for {user.username}.")


@router.post("/accounts/{user_id}/reset-device", response_model=MessageOut)
async def reset_device(
    user_id: str,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    """Unbind every device so the account can bind afresh on its next login (§6, §9)."""
    user = await _load_staff(db, user_id, (Role.MASTER, Role.SELLER))
    removed = await device_mod.reset_devices(db, user.id)
    await sess.revoke_all_for_user(db, user.id, sess.STAFF)
    await audit.log(
        db, action="account.reset_device", actor=principal, request=request, target_type="user",
        target_id=user.id, summary=f"Reset device binding for {user.label}",
        meta={"devices_removed": removed},
    )
    await db.commit()
    return MessageOut(
        message=f"Device binding cleared for {user.username}. The next login will bind a new device."
    )


@router.get("/accounts/{user_id}/devices")
async def account_devices(
    user_id: str,
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    user = await _load_staff(db, user_id, (Role.MASTER, Role.SELLER))
    devices = await device_mod.list_devices(db, user.id)
    return {"devices": [_serialise_device(d) for d in devices]}


@router.delete("/accounts/{user_id}", response_model=MessageOut)
async def delete_account(
    user_id: str,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    user = await _load_staff(db, user_id, (Role.MASTER, Role.SELLER))
    if _is_root(user):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The root master cannot be deleted.")
    if user.id == principal.user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your own account.")

    orders_held = int(
        (
            await db.execute(select(func.count(Order.id)).where(Order.seller_id == user.id))
        ).scalar()
        or 0
    )
    label = user.label
    if orders_held:
        # order history must survive, so the account is disabled and unbound instead
        user.is_active = False
        await device_mod.reset_devices(db, user.id)
        await sess.revoke_all_for_user(db, user.id)
        await audit.log(
            db, action="account.disable", actor=principal, request=request, target_type="user",
            target_id=user.id, summary=f"Disabled {label} (has {orders_held} orders)",
        )
        await db.commit()
        return MessageOut(
            message=(
                f"{label} handled {orders_held} order(s), so the account was disabled "
                "instead of deleted to keep the order history intact."
            )
        )

    await sess.revoke_all_for_user(db, user.id)
    await audit.log(
        db, action="account.delete", actor=principal, request=request, target_type="user",
        target_id=user.id, summary=f"Deleted account {label}",
    )
    await db.delete(user)
    await db.commit()
    return MessageOut(message=f"{label} was deleted.")
