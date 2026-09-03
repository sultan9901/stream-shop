"""STREAM CORPORATION — FastAPI application factory.

Wires the routers, static/media mounts, security headers, error pages and the
startup bootstrap (schema, root master, default coin packages / payment methods).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import STATIC_DIR, UPLOAD_ROOT, settings
from app.database import dispose_db, init_db
from app.routes import (
    auth_google,
    auth_staff,
    catalog,
    download,
    master_accounts,
    master_catalog,
    master_customers,
    master_review,
    master_store,
    notifications,
    orders,
    pages,
    seller,
    wallet,
    ws,
)
from app.services import seed
from app.templating import page_context, templates
from app.websocket.manager import manager

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("stream.main")

ERROR_TITLES = {
    400: "Bad request",
    401: "Sign-in required",
    403: "Access denied",
    404: "Page not found",
    409: "Conflict",
    410: "Link expired",
    422: "Invalid data",
    429: "Too many requests",
    500: "System fault",
    503: "Service unavailable",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    report = await seed.run_bootstrap()
    log.info("bootstrap: %s", report)
    await manager.start_pubsub()
    try:
        yield
    finally:
        await manager.shutdown()
        await dispose_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        description="Premium coin-based software marketplace.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
    )

    if settings.host_list and settings.host_list != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.host_list)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Device-Id", "X-Requested-With"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-XSS-Protection", "0")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data: blob: https:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # only ``uploads/media`` is public — screenshots and product files are never
    # served statically (§32, §46).
    app.mount("/media", StaticFiles(directory=str(UPLOAD_ROOT / "media")), name="media")

    for module in (
        auth_staff, auth_google, catalog, wallet, orders, download, notifications,
        master_accounts, master_catalog, master_store, master_review, master_customers,
        seller, ws, pages,
    ):
        app.include_router(module.router)

    _install_error_handlers(app)
    return app


def _wants_json(request: Request) -> bool:
    path = request.url.path
    if path.startswith(("/api/", "/download/", "/ws")):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _install_error_handlers(app: FastAPI) -> None:
    async def html_error(request: Request, code: int, message: str) -> HTMLResponse:
        ctx = await page_context(
            request,
            None,
            page="error",
            code=code,
            title=ERROR_TITLES.get(code, "Something went wrong"),
            message=message,
            no_intro=True,
            robots="noindex, nofollow",
        )
        return templates.TemplateResponse(request, "error.html", ctx, status_code=code)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if _wants_json(request):
            body = detail if isinstance(detail, dict) else {"message": str(detail)}
            payload = {"ok": False, "status": exc.status_code, "detail": detail}
            payload.update({k: v for k, v in body.items() if k not in payload})
            return JSONResponse(
                payload, status_code=exc.status_code, headers=getattr(exc, "headers", None)
            )
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
        return await html_error(request, exc.status_code, message or "")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "input"
        message = f"{field}: {first.get('msg', 'invalid value')}"
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "status": 422, "detail": {"message": message},
                 "errors": exc.errors()},
                status_code=422,  # UNPROCESSABLE CONTENT; literal dodges the renamed-constant deprecation
            )
        return await html_error(request, 422, message)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        message = "An unexpected system fault occurred. The incident has been logged."
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "status": 500, "detail": {"message": message}}, status_code=500
            )
        return await html_error(request, 500, message)


app = create_app()
