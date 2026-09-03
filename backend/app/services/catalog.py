"""Catalogue reads + Master-side product/category mutation helpers."""
from __future__ import annotations

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.codes import slugify
from app.models.order import Order, OrderItem
from app.models.product import Category, Product, ProductFile, ProductMedia
from app.services import uploads

ACTIVE_ORDER_STATES = ("PENDING", "PAID", "PROCESSING", "COMPLETED")


class CatalogError(Exception):
    pass


# --------------------------------------------------------------------------
# slugs
# --------------------------------------------------------------------------
async def unique_slug(db: AsyncSession, model, name: str, *, exclude_id: str | None = None) -> str:
    base = slugify(name, 190)
    candidate = base
    suffix = 2
    while True:
        stmt = select(func.count(model.id)).where(model.slug == candidate)
        if exclude_id:
            stmt = stmt.where(model.id != exclude_id)
        if not int((await db.execute(stmt)).scalar() or 0):
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


# --------------------------------------------------------------------------
# categories
# --------------------------------------------------------------------------
async def list_categories(db: AsyncSession, *, only_active: bool = True) -> list[Category]:
    stmt = select(Category).order_by(Category.display_order, Category.name)
    if only_active:
        stmt = stmt.where(Category.is_active.is_(True))
    return list((await db.execute(stmt)).scalars())


async def category_counts(db: AsyncSession) -> dict[str, int]:
    rows = (
        await db.execute(
            select(Product.category_id, func.count(Product.id))
            .where(Product.is_active.is_(True))
            .group_by(Product.category_id)
        )
    ).all()
    return {str(cid): int(count) for cid, count in rows if cid}


def serialise_category(cat: Category, *, product_count: int | None = None) -> dict:
    return {
        "id": cat.id,
        "name": cat.name,
        "slug": cat.slug,
        "description": cat.description,
        "icon": cat.icon,
        "display_order": cat.display_order,
        "is_active": cat.is_active,
        "product_count": product_count,
    }


# --------------------------------------------------------------------------
# products
# --------------------------------------------------------------------------
def _product_query(*, with_media: bool = False):
    stmt = select(Product).options(
        selectinload(Product.category), selectinload(Product.seller), selectinload(Product.files)
    )
    if with_media:
        stmt = stmt.options(selectinload(Product.media))
    return stmt


async def list_products(
    db: AsyncSession,
    *,
    only_active: bool = True,
    category: str | None = None,
    seller_id: str | None = None,
    search: str | None = None,
    featured: bool | None = None,
    sort: str = "featured",
    limit: int = 60,
    offset: int = 0,
    with_media: bool = False,
) -> tuple[list[Product], int]:
    stmt = _product_query(with_media=with_media)
    count_stmt = select(func.count(Product.id))
    conds = []
    if only_active:
        conds.append(Product.is_active.is_(True))
    if category:
        conds.append(or_(Product.category_id == category, Category.slug == category))
        stmt = stmt.join(Category, Category.id == Product.category_id, isouter=True)
        count_stmt = count_stmt.join(Category, Category.id == Product.category_id, isouter=True)
    if seller_id:
        conds.append(Product.seller_id == seller_id)
    if featured is not None:
        conds.append(Product.is_featured.is_(featured))
    if search:
        like = f"%{search.strip()}%"
        conds.append(
            or_(Product.name.ilike(like), Product.tagline.ilike(like), Product.description.ilike(like))
        )
    for c in conds:
        stmt = stmt.where(c)
        count_stmt = count_stmt.where(c)

    total = int((await db.execute(count_stmt)).scalar() or 0)

    if sort == "price_asc":
        stmt = stmt.order_by(Product.coin_price.asc(), Product.name)
    elif sort == "price_desc":
        stmt = stmt.order_by(Product.coin_price.desc(), Product.name)
    elif sort == "newest":
        stmt = stmt.order_by(Product.created_at.desc())
    elif sort == "popular":
        stmt = stmt.order_by(Product.sold_count.desc(), Product.name)
    else:
        stmt = stmt.order_by(
            Product.is_featured.desc(), Product.display_order, Product.created_at.desc()
        )
    stmt = stmt.limit(min(limit, 200)).offset(max(offset, 0))
    return list((await db.execute(stmt)).scalars()), total


async def get_product(db: AsyncSession, ident: str) -> Product | None:
    stmt = _product_query(with_media=True).where(or_(Product.id == ident, Product.slug == ident))
    return (await db.execute(stmt)).scalars().first()


async def register_view(db: AsyncSession, product_id: str) -> None:
    await db.execute(
        update(Product).where(Product.id == product_id).values(view_count=Product.view_count + 1)
    )


async def owned_product_ids(db: AsyncSession, user_id: str) -> set[str]:
    rows = (
        await db.execute(
            select(OrderItem.product_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.user_id == user_id, Order.status.in_(ACTIVE_ORDER_STATES))
        )
    ).all()
    return {str(pid) for (pid,) in rows if pid}


def serialise_product(
    product: Product, *, detail: bool = False, owned: bool = False, staff: bool = False
) -> dict:
    gallery = [
        {"id": m.id, "kind": m.kind, "url": m.url, "caption": m.caption}
        for m in sorted(product.media or [], key=lambda m: m.display_order)
    ] if detail else []
    data = {
        "id": product.id,
        "name": product.name,
        "slug": product.slug,
        "tagline": product.tagline,
        "coin_price": int(product.coin_price),
        "version": product.version,
        "platform": product.platform,
        "thumbnail_url": product.thumbnail_url,
        "banner_url": product.banner_url,
        "demo_video_url": product.demo_video_url,
        "category": (
            {"id": product.category.id, "name": product.category.name, "slug": product.category.slug}
            if product.category
            else None
        ),
        "seller": (
            {"id": product.seller.id, "label": product.seller.label} if product.seller else None
        ),
        "is_active": product.is_active,
        "is_featured": product.is_featured,
        "allow_repurchase": product.allow_repurchase,
        "in_stock": product.in_stock,
        "stock": product.stock,
        "sold_count": int(product.sold_count),
        "owned": owned,
        "created_at": product.created_at.isoformat() if product.created_at else None,
    }
    if detail:
        data.update(
            {
                "description": product.description,
                "delivery_note": product.delivery_note,
                "gallery": gallery,
                "has_file": product.primary_file is not None,
                "has_external_link": bool(product.external_download_url),
            }
        )
    if staff:
        pfile = product.primary_file
        data.update(
            {
                "display_order": product.display_order,
                "view_count": int(product.view_count),
                "category_id": product.category_id,
                "seller_id": product.seller_id,
                "external_download_url": product.external_download_url,
                "description": product.description,
                "delivery_note": product.delivery_note,
                "file": (
                    {
                        "id": pfile.id,
                        "name": pfile.original_name,
                        "size_bytes": int(pfile.size_bytes),
                        "content_type": pfile.content_type,
                    }
                    if pfile
                    else None
                ),
                "media": [
                    {"id": m.id, "kind": m.kind, "url": m.url, "caption": m.caption,
                     "display_order": m.display_order}
                    for m in sorted(product.media or [], key=lambda m: m.display_order)
                ],
            }
        )
    return data


# --------------------------------------------------------------------------
# mutations (Master only — callers enforce RBAC)
# --------------------------------------------------------------------------
async def create_product(db: AsyncSession, payload) -> Product:
    product = Product(
        name=payload.name.strip(),
        slug=await unique_slug(db, Product, payload.name),
        coin_price=int(payload.coin_price),
    )
    await _apply_product_fields(db, product, payload)
    db.add(product)
    await db.flush()
    return product


async def update_product(db: AsyncSession, product: Product, payload) -> Product:
    if payload.name.strip() != product.name:
        product.name = payload.name.strip()
        product.slug = await unique_slug(db, Product, payload.name, exclude_id=product.id)
    product.coin_price = int(payload.coin_price)
    await _apply_product_fields(db, product, payload)
    await db.flush()
    return product


async def _apply_product_fields(db: AsyncSession, product: Product, payload) -> None:
    product.tagline = (payload.tagline or "").strip() or None
    product.description = payload.description or None
    product.version = (payload.version or "").strip() or None
    product.platform = (payload.platform or "").strip() or None
    product.category_id = payload.category_id or None
    product.seller_id = payload.seller_id or None
    product.external_download_url = (payload.external_download_url or "").strip() or None
    product.delivery_note = payload.delivery_note or None
    product.demo_video_url = (payload.demo_video_url or "").strip() or None
    product.is_active = bool(payload.is_active)
    product.is_featured = bool(payload.is_featured)
    product.allow_repurchase = bool(payload.allow_repurchase)
    product.stock = payload.stock
    product.display_order = int(payload.display_order or 0)


async def delete_product(db: AsyncSession, product: Product) -> None:
    """Products with order history are archived (deactivated) instead of deleted so
    the ledger and order records stay intact."""
    used = int(
        (
            await db.execute(
                select(func.count(OrderItem.id)).where(OrderItem.product_id == product.id)
            )
        ).scalar()
        or 0
    )
    if used:
        product.is_active = False
        product.is_featured = False
        await db.flush()
        raise CatalogError(
            "This product has order history, so it was deactivated instead of deleted."
        )
    for f in list(product.files or []):
        uploads.delete(f.stored_path)
    await db.delete(product)
    await db.flush()


async def attach_media(
    db: AsyncSession, product: Product, *, kind: str, url: str, caption: str | None = None
) -> ProductMedia:
    order = int(
        (
            await db.execute(
                select(func.coalesce(func.max(ProductMedia.display_order), -1)).where(
                    ProductMedia.product_id == product.id
                )
            )
        ).scalar()
        or -1
    )
    row = ProductMedia(
        product_id=product.id, kind=kind, url=url, caption=caption, display_order=order + 1
    )
    db.add(row)
    if kind == "image" and not product.thumbnail_url:
        product.thumbnail_url = url
    if kind == "banner":
        product.banner_url = url
    await db.flush()
    return row


async def replace_file(db: AsyncSession, product: Product, stored) -> ProductFile:
    for old in list(product.files or []):
        uploads.delete(old.stored_path)
        await db.delete(old)
    await db.flush()
    row = ProductFile(
        product_id=product.id,
        original_name=stored.original_name,
        stored_path=stored.stored_path,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        checksum_sha256=stored.checksum_sha256,
        is_primary=True,
    )
    db.add(row)
    await db.flush()
    return row
