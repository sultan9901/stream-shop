"""Jinja2 environment shared by ``app.main`` and ``app.routes.pages``."""
from __future__ import annotations

from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import TEMPLATES_DIR, settings
from app.services import settings_store

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["app_name"] = settings.app_name
templates.env.globals["year"] = __import__("datetime").datetime.now().year


async def page_context(request, db: AsyncSession | None = None, **extra) -> dict:
    """Base context for every server-rendered page."""
    values = await settings_store.all_settings(db) if db is not None else {}
    ctx = {
        "request": request,
        "app_name": settings.app_name,
        "tagline": values.get("site.tagline") or "Premium Coin-Based Software Marketplace",
        "announcement": values.get("site.announcement") or "",
        "support_email": values.get("site.support_email") or "",
        "support_whatsapp": values.get("site.support_whatsapp") or "",
        "seo_description": values.get("site.seo_description") or "",
        "seo_keywords": values.get("site.seo_keywords") or "",
        "intro_enabled": bool(values.get("intro.enabled", True)),
        "intro_duration": int(values.get("intro.duration_ms") or 3200),
        "google_enabled": settings.google_enabled,
        "dev_stub": settings.dev_stub_enabled,
        "base_url": settings.base_url.rstrip("/"),
        "debug": settings.debug,
    }
    ctx.update(extra)
    return ctx
