"""First-boot bootstrap: root master, coin packages, payment method, demo catalogue."""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.codes import slugify
from app.config import settings
from app.database import SessionLocal
from app.models.base import Role
from app.models.payment import PaymentMethod
from app.models.product import Category, Product
from app.models.user import User
from app.models.wallet import CoinPackage
from app.services import accounts, settings_store

log = logging.getLogger("stream.seed")

COIN_PACKAGES = [
    # name, coins, bonus, price BDT, badge
    ("Starter", 100, 0, 12, None),
    ("Basic", 500, 0, 55, None),
    ("Standard", 1000, 0, 100, "POPULAR"),
    ("Pro", 2500, 100, 230, "BEST VALUE"),
    ("Elite", 5000, 350, 440, None),
    ("Ultimate", 10000, 1000, 850, "MAX BONUS"),
]

CATEGORIES = [
    ("Security Tools", "shield"),
    ("Developer Tools", "code"),
    ("Automation", "bot"),
    ("Design Suite", "palette"),
]

DEMO_PRODUCTS = [
    {
        "name": "NEXUS Recon Suite",
        "tagline": "Authorised network reconnaissance & asset mapping toolkit",
        "coin_price": 1500,
        "version": "4.2.0",
        "category": "Security Tools",
        "platform": "Windows / Linux",
        "description": (
            "A licensed reconnaissance suite for authorised security assessments. Ships with "
            "asset discovery, service fingerprinting, report export and a scriptable API."
        ),
        "featured": True,
    },
    {
        "name": "QUANTUM Build Pipeline",
        "tagline": "Self-hosted CI/CD runner with zero-config caching",
        "coin_price": 3000,
        "version": "2.8.1",
        "category": "Developer Tools",
        "platform": "Docker / Linux",
        "description": (
            "Drop-in CI runner with content-addressed caching, matrix builds and signed artifacts. "
            "Includes a web dashboard and webhook integrations."
        ),
        "featured": True,
    },
    {
        "name": "PHANTOM Automation Studio",
        "tagline": "Visual workflow automation for repetitive desktop work",
        "coin_price": 500,
        "version": "1.9.3",
        "category": "Automation",
        "platform": "Windows",
        "description": "Record, edit and schedule desktop workflows with a node-based editor.",
    },
    {
        "name": "PRISM Design Kit",
        "tagline": "3,000+ cyber-grade UI components and vector assets",
        "coin_price": 1200,
        "version": "6.0.0",
        "category": "Design Suite",
        "platform": "Figma / SVG",
        "description": "A production design system: components, icons, gradients and motion presets.",
    },
]


async def ensure_root_master(db: AsyncSession) -> User | None:
    count = int((await db.execute(select(func.count(User.id)).where(User.role == Role.MASTER))).scalar() or 0)
    if count:
        return None
    user = await accounts.create_master(
        db,
        username=settings.default_master_username,
        password=settings.default_master_password,
        is_root=True,
        device_lock=True,
        must_change_password=True,
        # The spec pins the first-boot credentials to Admin / admin (§4), which is
        # shorter than the normal minimum. must_change_password above forces the
        # real password to be set before anything else can be done.
        allow_weak_password=True,
        note="Bootstrap root master created on first launch.",
    )
    log.warning(
        "Created bootstrap MASTER '%s' — change this password immediately at /master",
        settings.default_master_username,
    )
    return user


async def ensure_coin_packages(db: AsyncSession) -> None:
    if int((await db.execute(select(func.count(CoinPackage.id)))).scalar() or 0):
        return
    for order, (name, coins, bonus, price, badge) in enumerate(COIN_PACKAGES):
        db.add(
            CoinPackage(
                name=name, coins=coins, bonus_coins=bonus, price_bdt=price,
                badge=badge, is_active=True, display_order=order,
            )
        )
    await db.flush()


async def ensure_payment_methods(db: AsyncSession) -> None:
    if int((await db.execute(select(func.count(PaymentMethod.id)))).scalar() or 0):
        return
    db.add(
        PaymentMethod(
            name="bKash", account_number="01XXXXXXXXX", account_name="STREAM CORPORATION",
            account_type="Personal", is_active=True, display_order=0,
            instructions="Send Money (not Cash Out) to the number above, then upload the screenshot.",
        )
    )
    db.add(
        PaymentMethod(
            name="Nagad", account_number="01XXXXXXXXX", account_name="STREAM CORPORATION",
            account_type="Personal", is_active=True, display_order=1,
        )
    )
    await db.flush()


async def ensure_catalogue(db: AsyncSession) -> None:
    if int((await db.execute(select(func.count(Product.id)))).scalar() or 0):
        return
    cats: dict[str, Category] = {}
    for order, (name, icon) in enumerate(CATEGORIES):
        cat = Category(name=name, slug=slugify(name), icon=icon, display_order=order, is_active=True)
        db.add(cat)
        cats[name] = cat
    await db.flush()

    for order, spec in enumerate(DEMO_PRODUCTS):
        db.add(
            Product(
                name=spec["name"],
                slug=slugify(spec["name"]),
                tagline=spec["tagline"],
                description=spec["description"],
                version=spec["version"],
                platform=spec["platform"],
                coin_price=spec["coin_price"],
                category_id=cats[spec["category"]].id,
                is_active=True,
                is_featured=bool(spec.get("featured")),
                display_order=order,
                delivery_note="Activation instructions are included inside the archive.",
                external_download_url=None,
            )
        )
    await db.flush()


async def run_bootstrap() -> dict:
    """Idempotent: safe to call on every startup."""
    result = {"master_created": False}
    async with SessionLocal() as db:
        async with db.begin():
            master = await ensure_root_master(db)
            result["master_created"] = master is not None
            await ensure_coin_packages(db)
            await ensure_payment_methods(db)
            await ensure_catalogue(db)
            await settings_store.ensure_defaults(db)
    return result
