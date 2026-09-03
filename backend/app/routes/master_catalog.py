"""Master panel — catalogue administration: categories, products, media, files (§35–§37)."""
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.deps import Principal, csrf_master, require_master
from app.database import get_db
from app.models.product import Category, Product, ProductMedia
from app.schemas.admin import CategoryIn, ProductIn
from app.schemas.auth import MessageOut
from app.services import audit, catalog, uploads

router = APIRouter(prefix="/api/master", tags=["master-catalog"])

MEDIA_KINDS = ("image", "gallery", "banner", "thumbnail")


async def _load_product(db: AsyncSession, product_id: str) -> Product:
    product = (
        await db.execute(
            select(Product)
            .where(Product.id == product_id)
            .options(
                selectinload(Product.media),
                selectinload(Product.files),
                selectinload(Product.category),
                selectinload(Product.seller),
            )
        )
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found.")
    return product


# ---------------------------------------------------------------- categories
@router.get("/categories")
async def list_categories(
    _: Principal = Depends(require_master), db: AsyncSession = Depends(get_db)
) -> dict:
    rows = await catalog.list_categories(db, only_active=False)
    counts = await catalog.category_counts(db)
    return {
        "categories": [
            catalog.serialise_category(c, product_count=counts.get(c.id, 0)) for c in rows
        ]
    }


@router.post("/categories", status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: CategoryIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = Category(
        name=payload.name.strip(),
        slug=await catalog.unique_slug(db, Category, payload.name),
        description=payload.description or None,
        icon=(payload.icon or "").strip() or None,
        display_order=int(payload.display_order or 0),
        is_active=bool(payload.is_active),
    )
    db.add(row)
    await db.flush()
    await audit.log(
        db, action="catalog.category_create", actor=principal, request=request,
        target_type="category", target_id=row.id, summary=f"Created category {row.name}",
    )
    await db.commit()
    return {"ok": True, "category": catalog.serialise_category(row, product_count=0)}


@router.put("/categories/{category_id}")
async def update_category(
    category_id: str,
    payload: CategoryIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (
        await db.execute(select(Category).where(Category.id == category_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found.")
    if payload.name.strip() != row.name:
        row.name = payload.name.strip()
        row.slug = await catalog.unique_slug(db, Category, payload.name, exclude_id=row.id)
    row.description = payload.description or None
    row.icon = (payload.icon or "").strip() or None
    row.display_order = int(payload.display_order or 0)
    row.is_active = bool(payload.is_active)
    await db.flush()
    await audit.log(
        db, action="catalog.category_update", actor=principal, request=request,
        target_type="category", target_id=row.id, summary=f"Updated category {row.name}",
    )
    await db.commit()
    counts = await catalog.category_counts(db)
    return {"ok": True, "category": catalog.serialise_category(row, product_count=counts.get(row.id, 0))}


@router.delete("/categories/{category_id}", response_model=MessageOut)
async def delete_category(
    category_id: str,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    row = (
        await db.execute(select(Category).where(Category.id == category_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found.")
    used = int(
        (
            await db.execute(
                select(func.count(Product.id)).where(Product.category_id == row.id)
            )
        ).scalar()
        or 0
    )
    if used:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{used} product(s) still use this category. Move them first.",
        )
    name = row.name
    await db.delete(row)
    await audit.log(
        db, action="catalog.category_delete", actor=principal, request=request,
        target_type="category", target_id=category_id, summary=f"Deleted category {name}",
    )
    await db.commit()
    return MessageOut(message=f"Category {name} deleted.")


# ---------------------------------------------------------------- products
@router.get("/products")
async def list_products(
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None, max_length=120),
    category: str | None = Query(default=None, max_length=64),
    seller_id: str | None = Query(default=None, max_length=64),
    sort: str = Query(default="newest", max_length=20),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await catalog.list_products(
        db, only_active=False, category=category, seller_id=seller_id, search=q,
        sort=sort, limit=limit, offset=offset, with_media=True,
    )
    return {
        "total": total,
        "products": [catalog.serialise_product(p, staff=True) for p in rows],
    }


@router.get("/products/{product_id}")
async def product_detail(
    product_id: str,
    _: Principal = Depends(require_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    product = await _load_product(db, product_id)
    return {"product": catalog.serialise_product(product, detail=True, staff=True)}


@router.post("/products", status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        product = await catalog.create_product(db, payload)
    except catalog.CatalogError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await audit.log(
        db, action="catalog.product_create", actor=principal, request=request,
        target_type="product", target_id=product.id,
        summary=f"Created product {product.name} ({product.coin_price} coins)",
    )
    await db.commit()
    fresh = await _load_product(db, product.id)
    return {"ok": True, "product": catalog.serialise_product(fresh, detail=True, staff=True)}


@router.put("/products/{product_id}")
async def update_product(
    product_id: str,
    payload: ProductIn,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    product = await _load_product(db, product_id)
    before = {"coin_price": product.coin_price, "is_active": product.is_active,
              "seller_id": product.seller_id}
    try:
        await catalog.update_product(db, product, payload)
    except catalog.CatalogError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await audit.log(
        db, action="catalog.product_update", actor=principal, request=request,
        target_type="product", target_id=product.id,
        summary=f"Updated product {product.name}",
        meta={"before": before, "after": {"coin_price": product.coin_price,
                                          "is_active": product.is_active,
                                          "seller_id": product.seller_id}},
    )
    await db.commit()
    fresh = await _load_product(db, product.id)
    return {"ok": True, "product": catalog.serialise_product(fresh, detail=True, staff=True)}


@router.delete("/products/{product_id}", response_model=MessageOut)
async def delete_product(
    product_id: str,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    product = await _load_product(db, product_id)
    name = product.name
    try:
        await catalog.delete_product(db, product)
    except catalog.CatalogError as exc:
        # products with order history are archived, not deleted — that is a success
        await audit.log(
            db, action="catalog.product_archive", actor=principal, request=request,
            target_type="product", target_id=product_id, summary=f"Archived product {name}",
        )
        await db.commit()
        return MessageOut(message=str(exc))
    await audit.log(
        db, action="catalog.product_delete", actor=principal, request=request,
        target_type="product", target_id=product_id, summary=f"Deleted product {name}",
    )
    await db.commit()
    return MessageOut(message=f"Product {name} deleted.")


# ---------------------------------------------------------------- media & files
@router.post("/products/{product_id}/media", status_code=status.HTTP_201_CREATED)
async def upload_media(
    product_id: str,
    request: Request,
    kind: str = Form(default="image"),
    caption: str | None = Form(default=None),
    image: UploadFile = File(...),
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Upload a product image / gallery shot / banner. Stored under ``uploads/media``,
    which is the only publicly served upload folder."""
    kind = (kind or "image").strip().lower()
    if kind not in MEDIA_KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"kind must be one of: {', '.join(MEDIA_KINDS)}"
        )
    product = await _load_product(db, product_id)
    stored = await uploads.save_image(image, folder="media/products")
    url = f"/{stored.stored_path}"
    try:
        if kind == "thumbnail":
            product.thumbnail_url = url
            await db.flush()
            row = None
        else:
            row = await catalog.attach_media(
                db, product, kind="image" if kind == "gallery" else kind, url=url,
                caption=(caption or "").strip() or None,
            )
        await audit.log(
            db, action="catalog.media_upload", actor=principal, request=request,
            target_type="product", target_id=product.id,
            summary=f"Uploaded {kind} for {product.name}", meta={"url": url},
        )
        await db.commit()
    except Exception:
        await db.rollback()
        uploads.delete(stored.stored_path)
        raise
    return {
        "ok": True,
        "media": {"id": row.id if row else None, "kind": kind, "url": url},
        "product": catalog.serialise_product(await _load_product(db, product.id), detail=True, staff=True),
    }


@router.delete("/products/{product_id}/media/{media_id}", response_model=MessageOut)
async def delete_media(
    product_id: str,
    media_id: str,
    request: Request,
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    row = (
        await db.execute(
            select(ProductMedia).where(
                ProductMedia.id == media_id, ProductMedia.product_id == product_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Media not found.")
    product = await _load_product(db, product_id)
    url = row.url
    await db.delete(row)
    if product.thumbnail_url == url:
        product.thumbnail_url = None
    if product.banner_url == url:
        product.banner_url = None
    await db.flush()
    if url.startswith("/media/"):
        uploads.delete(url.lstrip("/"))
    await audit.log(
        db, action="catalog.media_delete", actor=principal, request=request,
        target_type="product", target_id=product_id, summary=f"Removed media from {product.name}",
        meta={"url": url},
    )
    await db.commit()
    return MessageOut(message="Media removed.")


@router.post("/products/{product_id}/file", status_code=status.HTTP_201_CREATED)
async def upload_product_file(
    product_id: str,
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(csrf_master),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Attach the deliverable itself. Never served statically — only through a
    per-order expiring download token (§32)."""
    product = await _load_product(db, product_id)
    stored = await uploads.save_product_file(file)
    try:
        row = await catalog.replace_file(db, product, stored)
        await audit.log(
            db, action="catalog.file_upload", actor=principal, request=request,
            target_type="product", target_id=product.id,
            summary=f"Attached file {stored.original_name} to {product.name}",
            meta={"size_bytes": stored.size_bytes, "sha256": stored.checksum_sha256},
        )
        await db.commit()
    except Exception:
        await db.rollback()
        uploads.delete(stored.stored_path)
        raise
    return {
        "ok": True,
        "file": {
            "id": row.id,
            "original_name": row.original_name,
            "size_bytes": row.size_bytes,
            "content_type": row.content_type,
        },
    }
