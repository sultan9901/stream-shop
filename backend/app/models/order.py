"""Orders, order items, delivery records, secure download tokens."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, DeliveryStatus, OrderStatus, TimestampMixin, TZDateTime, UUIDPk
from app.models.product import Product
from app.models.user import User


class Order(UUIDPk, TimestampMixin, Base):
    __tablename__ = "orders"

    order_code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seller_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    coin_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=OrderStatus.PENDING, nullable=False, index=True
    )
    customer_email: Mapped[str | None] = mapped_column(String(255))
    customer_label: Mapped[str | None] = mapped_column(String(160))

    debit_txn_id: Mapped[str | None] = mapped_column(String(32))
    refund_txn_id: Mapped[str | None] = mapped_column(String(32))

    paid_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    refunded_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    refund_reason: Mapped[str | None] = mapped_column(Text)
    seller_note: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    seller: Mapped[User | None] = relationship(foreign_keys=[seller_id])
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    deliveries: Mapped[list["Delivery"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_orders_user_status", "user_id", "status"),)

    @property
    def primary_item(self) -> "OrderItem | None":
        return self.items[0] if self.items else None

    @property
    def is_active(self) -> bool:
        return self.status in {
            OrderStatus.PENDING,
            OrderStatus.PAID,
            OrderStatus.PROCESSING,
            OrderStatus.COMPLETED,
        }


class OrderItem(UUIDPk, TimestampMixin, Base):
    __tablename__ = "order_items"

    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    product_version: Mapped[str | None] = mapped_column(String(40))
    coin_price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product | None] = relationship()


class Delivery(UUIDPk, TimestampMixin, Base):
    """One delivery attempt-set per order. ``idempotency_key`` is unique so the
    same order can never trigger two delivery emails."""

    __tablename__ = "deliveries"

    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(24), default="EMAIL", nullable=False)
    email_to: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(24), default=DeliveryStatus.QUEUED, nullable=False, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)

    order: Mapped[Order] = relationship(back_populates="deliveries")

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_deliveries_idempotency_key"),
    )


class DownloadToken(UUIDPk, TimestampMixin, Base):
    """Per-order, expiring, single-customer download grant. Only the HMAC of the
    token is stored, so the DB never holds a usable download credential."""

    __tablename__ = "download_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_file_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("product_files.id", ondelete="SET NULL")
    )
    external_url: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    max_downloads: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_downloaded_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class DownloadLog(UUIDPk, TimestampMixin, Base):
    __tablename__ = "download_logs"

    token_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("download_tokens.id", ondelete="SET NULL"), index=True
    )
    order_id: Mapped[str | None] = mapped_column(String(32), index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
