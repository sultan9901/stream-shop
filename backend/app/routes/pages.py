"""Server-rendered pages: the three surfaces (`/`, `/master`, `/seller`), viewer
sub-pages, SEO endpoints and error pages.

The panels are separate URLs with separate templates, but they all talk to the same
API and the same database. Authorisation is *never* decided here — every panel
fetches its data from an endpoint guarded by ``require_master`` / ``require_seller``,
so a viewer who opens /master simply gets a login screen and empty data.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.product import Product
from app.services import catalog, settings_store
from app.templating import page_context, templates
from app.wallet import service as wallet_service

router = APIRouter(include_in_schema=False)


async def _render(name: str, request: Request, db: AsyncSession, **extra) -> HTMLResponse:
    ctx = await page_context(request, db, **extra)
    return templates.TemplateResponse(request, name, ctx)


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    categories = await catalog.list_categories(db)
    counts = await catalog.category_counts(db)
    featured, _ = await catalog.list_products(db, featured=True, limit=6)
    return await _render(
        "index.html", request, db,
        page="home",
        categories=[
            catalog.serialise_category(c, product_count=counts.get(c.id, 0)) for c in categories
        ],
        featured=[catalog.serialise_product(p) for p in featured],
    )


@router.get("/product/{slug}", response_class=HTMLResponse)
async def product_page(
    slug: str, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    product = await catalog.get_product(db, slug)
    if product is None or not product.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found.")
    data = catalog.serialise_product(product, detail=True)
    return await _render(
        "product.html", request, db, page="product", product=data,
        page_title=f"{product.name} — {settings.app_name}",
        page_description=(product.tagline or product.name)[:180],
    )


@router.get("/wallet", response_class=HTMLResponse)
async def wallet_page(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return await _render("wallet.html", request, db, page="wallet", page_title="My Wallet")


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return await _render("orders.html", request, db, page="orders", page_title="My Orders")


@router.get("/master", response_class=HTMLResponse)
async def master_panel(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return await _render(
        "master.html", request, db, page="master", page_title="Master Control",
        robots="noindex, nofollow",
    )


@router.get("/seller", response_class=HTMLResponse)
async def seller_panel(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return await _render(
        "seller.html", request, db, page="seller", page_title="Seller Console",
        robots="noindex, nofollow",
    )


# ------------------------------------------------------------------ SEO
@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots() -> PlainTextResponse:
    base = settings.base_url.rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /master\n"
        "Disallow: /seller\n"
        "Disallow: /api/\n"
        "Disallow: /download/\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return PlainTextResponse(body)


@router.get("/sitemap.xml")
async def sitemap(db: AsyncSession = Depends(get_db)) -> Response:
    base = settings.base_url.rstrip("/")
    rows = list(
        (
            await db.execute(
                select(Product.slug, Product.updated_at).where(Product.is_active.is_(True))
            )
        ).all()
    )
    urls = [f"  <url><loc>{base}/</loc><changefreq>daily</changefreq></url>"]
    for slug, updated in rows:
        stamp = f"<lastmod>{updated.date().isoformat()}</lastmod>" if updated else ""
        urls.append(f"  <url><loc>{base}/product/{slug}</loc>{stamp}</url>")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )
    return Response(xml, media_type="application/xml")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Inline SVG favicon so no binary asset is required."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#05070f"/>'
        '<text x="32" y="43" font-family="monospace" font-size="34" font-weight="bold" '
        'text-anchor="middle" fill="#00f0ff">S</text></svg>'
    )
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


# ------------------------------------------------------------------ health
@router.get("/health", include_in_schema=True)
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    """Liveness, a cheap settings read that proves the DB answers, and ledger integrity.

    The wallet block is the one that matters operationally: it re-proves that every
    cached balance still equals the sum of its append-only ledger rows. Point an
    uptime monitor at this and a balance written outside the ledger path becomes an
    alert instead of a surprise.
    """
    ledger: dict = {"checked": False}
    try:
        await settings_store.get(db, "site.tagline")
        db_ok = True
    except Exception:  # pragma: no cover - only when the DB is down
        db_ok = False
    if db_ok:
        try:
            ledger = {"checked": True, **await wallet_service.audit_consistency_all(db)}
        except Exception:  # pragma: no cover - never fail the probe on the deep check
            ledger = {"checked": False}
    return {
        "ok": db_ok and ledger.get("consistent", True),
        "app": settings.app_name,
        "environment": settings.environment,
        "database": "up" if db_ok else "down",
        "wallet_ledger": ledger,
    }
