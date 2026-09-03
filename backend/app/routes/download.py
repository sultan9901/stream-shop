"""Secure product downloads + staff-only screenshot serving (spec §32).

There is no public, permanent download URL. Every download goes through a
per-order token that is hashed in the database, expires, has a download ceiling,
is revoked on refund, and logs every attempt.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import sessions as sess
from app.auth.deps import Principal, optional_viewer, require_staff
from app.auth.ratelimit import enforce
from app.database import get_db
from app.delivery import tokens as download_tokens
from app.models.payment import PaymentScreenshot
from app.models.product import ProductFile
from app.services import uploads

log = logging.getLogger("stream.download")
router = APIRouter(tags=["download"])

REASONS = {
    "invalid": (status.HTTP_404_NOT_FOUND, "This download link is not valid."),
    "expired": (status.HTTP_410_GONE, "This download link has expired. Request a new one from your orders page."),
    "revoked": (status.HTTP_403_FORBIDDEN, "This download link was revoked (the order was refunded)."),
    "exhausted": (status.HTTP_429_TOO_MANY_REQUESTS, "This download link has reached its download limit."),
}


@router.get("/download/{raw_token}")
async def download(
    raw_token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal | None = Depends(optional_viewer),
):
    await enforce(request, "download", "60/1h")
    ip = sess.client_ip(request)
    ua = request.headers.get("user-agent")

    token, reason = await download_tokens.resolve(db, raw_token)
    if reason:
        await download_tokens.log_attempt(
            db, token=token, outcome=reason, ip=ip, user_agent=ua,
            user_id=principal.user.id if principal else None,
        )
        await db.commit()
        code, message = REASONS.get(reason, REASONS["invalid"])
        raise HTTPException(code, message)

    assert token is not None
    # A signed-in viewer must be the grant's owner. An anonymous visitor is
    # allowed because the token itself was emailed only to that customer.
    if principal is not None and principal.user.id != token.user_id:
        await download_tokens.log_attempt(
            db, token=token, outcome="forbidden", ip=ip, user_agent=ua, user_id=principal.user.id
        )
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This download belongs to another account.")

    if token.product_file_id:
        pfile = (
            await db.execute(select(ProductFile).where(ProductFile.id == token.product_file_id))
        ).scalar_one_or_none()
        if pfile is None:
            await download_tokens.log_attempt(
                db, token=token, outcome="missing_file", ip=ip, user_agent=ua
            )
            await db.commit()
            raise HTTPException(status.HTTP_410_GONE, "The file for this order is no longer available.")

        path = uploads.resolve(pfile.stored_path)
        if not path.is_file():
            await download_tokens.log_attempt(
                db, token=token, outcome="missing_file", ip=ip, user_agent=ua
            )
            await db.commit()
            log.error("download token %s points at a missing file %s", token.id, pfile.stored_path)
            raise HTTPException(status.HTTP_410_GONE, "The file for this order is no longer available.")

        await download_tokens.register_hit(db, token)
        await download_tokens.log_attempt(db, token=token, outcome="ok", ip=ip, user_agent=ua)
        await db.commit()
        return FileResponse(
            path,
            media_type=pfile.content_type or "application/octet-stream",
            filename=pfile.original_name,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    if token.external_url:
        await download_tokens.register_hit(db, token)
        await download_tokens.log_attempt(
            db, token=token, outcome="ok_external", ip=ip, user_agent=ua
        )
        await db.commit()
        return RedirectResponse(token.external_url, status_code=status.HTTP_302_FOUND)

    await download_tokens.log_attempt(db, token=token, outcome="no_payload", ip=ip, user_agent=ua)
    await db.commit()
    raise HTTPException(
        status.HTTP_409_CONFLICT, "No download has been attached to this product yet."
    )


@router.get("/api/payments/screenshot/{screenshot_id}")
async def payment_screenshot(
    screenshot_id: str,
    principal: Principal = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
):
    """Payment proofs are never public — Master/Seller only."""
    row = (
        await db.execute(select(PaymentScreenshot).where(PaymentScreenshot.id == screenshot_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Screenshot not found.")
    path = uploads.resolve(row.stored_path)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "Screenshot file is missing from storage.")
    return FileResponse(
        path,
        media_type=row.content_type or "image/png",
        headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"},
    )
