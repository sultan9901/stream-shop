"""Declarative base + shared column mixins and enums."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData, String, TypeDecorator, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator):
    """A timezone-aware timestamp that behaves identically on SQLite and Postgres.

    SQLite has no native timezone storage, so SQLAlchemy hands back *naive*
    datetimes — and the moment one of those is compared against the tz-aware
    :func:`utcnow` (session expiry, download-token expiry, account lockout, …) the
    interpreter raises ``TypeError: can't compare offset-naive and offset-aware``.
    This decorator writes everything as UTC and guarantees an aware UTC value on
    the way back out, so the same comparison is safe on both backends. On Postgres
    the underlying column is still ``TIMESTAMP WITH TIME ZONE``.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value



def new_uuid() -> str:
    return uuid.uuid4().hex


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class UUIDPk:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)


# --------------------------------------------------------------------------
# Enumerations (stored as short VARCHARs so they are readable in the DB)
# --------------------------------------------------------------------------
class StrEnum(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover
        return self.value


class Role(StrEnum):
    MASTER = "MASTER"
    SELLER = "SELLER"
    VIEWER = "VIEWER"


class TxnType(StrEnum):
    COIN_PURCHASE = "COIN_PURCHASE"
    COIN_SPENT = "COIN_SPENT"
    COIN_REFUND = "COIN_REFUND"
    BONUS_COIN = "BONUS_COIN"
    ADMIN_CREDIT = "ADMIN_CREDIT"
    ADMIN_DEBIT = "ADMIN_DEBIT"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class DeliveryStatus(StrEnum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class NotificationKind(StrEnum):
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    PAYMENT_REJECTED = "PAYMENT_REJECTED"
    COINS_ADDED = "COINS_ADDED"
    PRODUCT_PURCHASED = "PRODUCT_PURCHASED"
    PRODUCT_DELIVERED = "PRODUCT_DELIVERED"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    ORDER_COMPLETED = "ORDER_COMPLETED"
    ORDER_REFUNDED = "ORDER_REFUNDED"
    NEW_COIN_PAYMENT = "NEW_COIN_PAYMENT"
    NEW_PRODUCT_ORDER = "NEW_PRODUCT_ORDER"
    NEW_CUSTOMER = "NEW_CUSTOMER"
    SYSTEM = "SYSTEM"
