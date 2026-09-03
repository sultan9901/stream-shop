"""Account provisioning: masters, sellers, and Google viewers."""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.codes import next_code
from app.auth.security import hash_password
from app.models.base import NotificationKind, Role, utcnow
from app.models.user import MasterAccount, SellerAccount, User, ViewerProfile
from app.notifications import service as notify
from app.wallet import service as wallet_service

MIN_PASSWORD_LEN = 6


class AccountError(Exception):
    pass


async def username_taken(db: AsyncSession, username: str, exclude_id: str | None = None) -> bool:
    stmt = select(func.count(User.id)).where(func.lower(User.username) == username.strip().lower())
    if exclude_id:
        stmt = stmt.where(User.id != exclude_id)
    return bool((await db.execute(stmt)).scalar())


def validate_password(password: str) -> str:
    password = (password or "").strip()
    if len(password) < MIN_PASSWORD_LEN:
        raise AccountError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    return password


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not (3 <= len(username) <= 64):
        raise AccountError("Username must be 3–64 characters.")
    if not all(c.isalnum() or c in "._-" for c in username):
        raise AccountError("Username may only contain letters, digits, dot, underscore and dash.")
    return username


async def create_master(
    db: AsyncSession,
    *,
    username: str,
    password: str,
    created_by: User | None = None,
    is_root: bool = False,
    device_lock: bool = True,
    note: str | None = None,
    must_change_password: bool = False,
    allow_weak_password: bool = False,
) -> User:
    username = validate_username(username)
    # ``allow_weak_password`` exists for exactly one caller: the first-boot seed.
    # The spec fixes the default master credentials at Admin / admin (§4) and that
    # password is 5 characters, one short of MIN_PASSWORD_LEN. Rather than weaken
    # the rule for everyone, the seed opts out and sets must_change_password=True,
    # so the very first login is forced through the change-password screen.
    password = (password or "").strip() if allow_weak_password else validate_password(password)
    if not password:
        raise AccountError("Password must not be empty.")
    if await username_taken(db, username):
        raise AccountError("That username is already in use.")

    user = User(
        role=Role.MASTER,
        username=username,
        password_hash=hash_password(password),
        public_code=await next_code(db, "master"),
        is_active=True,
        device_lock_enabled=device_lock,
        must_change_password=must_change_password,
        display_name=username,
        created_by_id=created_by.id if created_by else None,
    )
    db.add(user)
    await db.flush()
    db.add(MasterAccount(user_id=user.id, is_root=is_root, can_manage_masters=True, note=note))
    await db.flush()
    return user


async def create_seller(
    db: AsyncSession,
    *,
    username: str,
    password: str,
    created_by: User | None = None,
    contact_email: str | None = None,
    device_lock: bool = True,
    can_verify_payments: bool = True,
    note: str | None = None,
) -> User:
    username = validate_username(username)
    password = validate_password(password)
    if await username_taken(db, username):
        raise AccountError("That username is already in use.")

    code = await next_code(db, "seller")
    user = User(
        role=Role.SELLER,
        username=username,
        password_hash=hash_password(password),
        public_code=code,
        is_active=True,
        device_lock_enabled=device_lock,
        display_name=username,
        email=(contact_email or "").strip().lower() or None,
        created_by_id=created_by.id if created_by else None,
    )
    db.add(user)
    await db.flush()
    db.add(
        SellerAccount(
            user_id=user.id,
            seller_code=code,
            contact_email=(contact_email or "").strip().lower() or None,
            can_verify_payments=can_verify_payments,
            note=note,
        )
    )
    await db.flush()
    return user


async def get_or_create_viewer(db: AsyncSession, profile: dict) -> tuple[User, bool]:
    """Upsert the Google identity. Returns ``(user, created)``."""
    sub = profile["sub"]
    email = profile["email"]

    user = (
        await db.execute(
            select(User)
            .where(or_(User.google_sub == sub, func.lower(User.email) == email))
            .where(User.role == Role.VIEWER)
            .options(selectinload(User.viewer_profile))
        )
    ).scalars().first()

    created = False
    if user is None:
        user = User(
            role=Role.VIEWER,
            email=email,
            google_sub=sub,
            display_name=profile.get("name") or email.split("@")[0],
            avatar_url=profile.get("picture"),
            public_code=await next_code(db, "customer"),
            is_active=True,
            device_lock_enabled=False,  # viewers may use any number of devices
        )
        db.add(user)
        await db.flush()
        db.add(
            ViewerProfile(
                user_id=user.id,
                customer_code=user.public_code,
                google_email=email,
                google_name=profile.get("name"),
                picture_url=profile.get("picture"),
                locale=profile.get("locale"),
                email_verified=bool(profile.get("email_verified")),
            )
        )
        created = True
    else:
        user.google_sub = sub
        user.email = email
        user.display_name = profile.get("name") or user.display_name
        user.avatar_url = profile.get("picture") or user.avatar_url
        vp = user.viewer_profile
        if vp is None:
            db.add(
                ViewerProfile(
                    user_id=user.id,
                    customer_code=user.public_code,
                    google_email=email,
                    google_name=profile.get("name"),
                    picture_url=profile.get("picture"),
                    email_verified=bool(profile.get("email_verified")),
                )
            )
        else:
            vp.google_email = email
            vp.google_name = profile.get("name") or vp.google_name
            vp.picture_url = profile.get("picture") or vp.picture_url
            vp.email_verified = bool(profile.get("email_verified")) or vp.email_verified

    user.last_login_at = utcnow()
    await db.flush()
    await wallet_service.get_or_create_wallet(db, user.id)

    if created:
        await notify.push(
            db,
            audience="MASTER",
            kind=NotificationKind.NEW_CUSTOMER,
            title="🔔 NEW CUSTOMER",
            body=f"{user.label} ({email}) just joined via Google.",
            icon="user",
            link=f"/master#customers/{user.id}",
            payload={"user_id": user.id},
        )
        await notify.push(
            db,
            user_id=user.id,
            kind=NotificationKind.SYSTEM,
            title="Welcome to STREAM CORPORATION",
            body=(
                f"{notify.BRAND}\n\nWelcome, {user.label}!\n\n"
                "Buy coins from your wallet, then use coins to unlock premium software.\n"
                "Every purchase is delivered straight to your Gmail."
            ),
            icon="spark",
            link="/wallet",
        )
    return user, created


def serialise_user(user: User, *, wallet_balance: int | None = None) -> dict:
    return {
        "id": user.id,
        "role": user.role,
        "code": user.public_code,
        "username": user.username,
        "email": user.email,
        "name": user.display_name,
        "avatar": user.avatar_url,
        "is_active": user.is_active,
        "device_lock": user.device_lock_enabled,
        "must_change_password": user.must_change_password,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "last_login_ip": user.last_login_ip,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "coin_balance": wallet_balance,
    }
