"""Request schemas for viewer-facing commerce endpoints."""
from __future__ import annotations

from pydantic import BaseModel, Field


class PurchaseIn(BaseModel):
    product_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=120)


class MarkReadIn(BaseModel):
    notification_id: str | None = Field(default=None, max_length=64)


class PaymentReviewIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class OrderNoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class RefundIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    cancel: bool = False
