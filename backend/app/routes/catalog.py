"""Public catalogue API — products and categories (no authentication required)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import Principal, optional_viewer
from app.database import get_db
from app.services import catalog
from app.services import settings_store

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/categories")
async def categories(db: AsyncSession = Depends(get_db)) -> dict:
    rows = await catalog.list_categories(db, only_active=True)
    counts = await catalog.category_counts(db)
    return {
        "categories": [
            catalog.serialise_category(c, product_count=counts.get(c.id, 0)) for c in rows
        ]
    }


@router.get("/products")
async def products(
    db: AsyncSession = Depends(get_db),
    principal: Principal | None = Depends(optional_viewer),
    category: str | None = Query(default=None, max_length=140),
    q: str | None = Query(default=None, max_length=120),
    featured: bool | None = Query(default=None),
    sort: str = Query(default="featured", pattern="^(featured|newest|popular|price_asc|price_desc)$"),
    limit: int = Query(default=48, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await catalog.list_products(
        db,
        only_active=True,
        category=category,
        search=q,
        featured=featured,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    owned = await catalog.owned_product_ids(db, principal.user.id) if principal else set()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "products": [
            catalog.serialise_product(p, owned=p.id in owned) for p in rows
        ],
    }


@router.get("/products/{ident}")
async def product_detail(
    ident: str,
    db: AsyncSession = Depends(get_db),
    principal: Principal | None = Depends(optional_viewer),
) -> dict:
    product = await catalog.get_product(db, ident)
    if product is None or not product.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That product could not be found.")

    owned = False
    balance = None
    if principal is not None:
        from app.wallet import service as wallet_service

        owned = product.id in await catalog.owned_product_ids(db, principal.user.id)
        balance = await wallet_service.balance_of(db, principal.user.id)

    await catalog.register_view(db, product.id)
    await db.commit()

    price = int(product.coin_price)
    data = catalog.serialise_product(product, detail=True, owned=owned)
    data["affordability"] = {
        "authenticated": principal is not None,
        "balance": balance,
        "required": price,
        "shortfall": max(price - balance, 0) if balance is not None else None,
        "can_afford": balance is not None and balance >= price,
    }
    return data


@router.get("/site")
async def site_config(
    db: AsyncSession = Depends(get_db), principal: Principal | None = Depends(optional_viewer)
) -> dict:
    """Everything the shell needs on first paint: branding, intro flags, auth mode."""
    from app.config import settings

    values = await settings_store.all_settings(db)
    return {
        "app_name": settings.app_name,
        "tagline": values.get("site.tagline"),
        "announcement": values.get("site.announcement") or None,
        "support_email": values.get("site.support_email"),
        "support_whatsapp": values.get("site.support_whatsapp") or None,
        "seo_description": values.get("site.seo_description"),
        "intro": {
            "enabled": bool(values.get("intro.enabled")),
            "duration_ms": int(values.get("intro.duration_ms") or 3200),
        },
        "auth": {
            "google_enabled": settings.google_enabled,
            "dev_stub": settings.dev_stub_enabled,
        },
        "authenticated": principal is not None,
    }
