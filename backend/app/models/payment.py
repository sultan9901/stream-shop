"""Manual (screenshot-verified) coin payment flow."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PaymentStatus, TimestampMixin, TZDateTime, UUIDPk
from app.models.user import User
from app.models.wallet import CoinPackage


class PaymentMethod(UUIDPk, TimestampMixin, Base):
    """Master-configured destination for BDT payments (bKash / Nagad / ...)."""

    __tablename__ = "payment_methods"

    name: Mapped[str] = mapped_column(String(80), nullable=False)        # bKash
    account_number: Mapped[str] = mapped_column(String(80), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(120))
    account_type: Mapped[str | None] = mapped_column(String(40))          # Personal / Agent
    instructions: Mapped[str | None] = mapped_column(Text)
    logo_url: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PaymentRequest(UUIDPk, TimestampMixin, Base):
    """A viewer's claim: "I sent ৳X, please credit my coins."

    State machine — PENDING -> CONFIRMED | REJECTED | CANCELLED. Transitions are
    performed with a conditional UPDATE (``WHERE status='PENDING'``) so a second
    reviewer clicking CONFIRM can never double-credit.
    """

    __tablename__ = "payment_requests"

    request_code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("coin_packages.id", ondelete="SET NULL")
    )
    payment_method_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("payment_methods.id", ondelete="SET NULL")
    )

    # snapshot of the package at request time (packages can be edited later)
    package_name: Mapped[str | None] = mapped_column(String(120))
    coins: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bonus_coins: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    amount_bdt: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    method_name: Mapped[str | None] = mapped_column(String(80))
    method_number: Mapped[str | None] = mapped_column(String(80))

    sender_number: Mapped[str | None] = mapped_column(String(80))
    transaction_ref: Mapped[str | None] = mapped_column(String(120), index=True)
    note: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(24), default=PaymentStatus.PENDING, nullable=False, index=True
    )
    reviewed_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_by_label: Mapped[str | None] = mapped_column(String(160))
    reviewed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    credited_txn_id: Mapped[str | None] = mapped_column(String(32))

    ip: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    package: Mapped[CoinPackage | None] = relationship()
    method: Mapped[PaymentMethod | None] = relationship()
    screenshots: Mapped[list["PaymentScreenshot"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_payment_requests_status_created", "status", "created_at"),)

    @property
    def total_coins(self) -> int:
        return int(self.coins) + int(self.bonus_coins)

    @property
    def is_pending(self) -> bool:
        return self.status == PaymentStatus.PENDING


class PaymentScreenshot(UUIDPk, TimestampMixin, Base):
    __tablename__ = "payment_screenshots"

    request_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("payment_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_name: Mapped[str | None] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(80))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), index=True)

    request: Mapped[PaymentRequest] = relationship(back_populates="screenshots")
