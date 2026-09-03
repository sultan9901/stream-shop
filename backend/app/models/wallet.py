"""Coin wallet + append-only ledger + coin packages.

The wallet balance is a *cached projection* of ``wallet_transactions``. Every coin
movement writes a ledger row inside the same DB transaction that updates the
cached balance, and every row carries a unique ``idempotency_key`` — so a retried
or double-clicked request can never credit/debit twice.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPk
from app.models.user import User


class CoinPackage(UUIDPk, TimestampMixin, Base):
    __tablename__ = "coin_packages"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    coins: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bonus_coins: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    price_bdt: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    badge: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def total_coins(self) -> int:
        return int(self.coins) + int(self.bonus_coins)


class Wallet(UUIDPk, TimestampMixin, Base):
    __tablename__ = "wallets"

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_credited: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_spent: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    is_frozen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship()
    transactions: Mapped[list["WalletTransaction"]] = relationship(
        back_populates="wallet", order_by="WalletTransaction.created_at.desc()"
    )


class WalletTransaction(UUIDPk, TimestampMixin, Base):
    """Append-only ledger row. `amount` is signed: >0 credit, <0 debit."""

    __tablename__ = "wallet_transactions"

    reference_code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    wallet_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    txn_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)

    bdt_amount: Mapped[float | None] = mapped_column(Numeric(12, 2))
    payment_method: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(24), default="COMPLETED", nullable=False)

    reference_type: Mapped[str | None] = mapped_column(String(40))  # payment_request | order | ...
    reference_id: Mapped[str | None] = mapped_column(String(32), index=True)

    # THE anti-duplication guard for money movements
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)

    reason: Mapped[str | None] = mapped_column(Text)
    performed_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )
    performed_by_label: Mapped[str | None] = mapped_column(String(160))

    wallet: Mapped[Wallet] = relationship(back_populates="transactions")

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_wallet_transactions_idempotency_key"),
        Index("ix_wallet_txn_user_created", "user_id", "created_at"),
    )
