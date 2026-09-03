"""Identity models: one `users` row per account, plus per-role profile tables."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Role, TimestampMixin, TZDateTime, UUIDPk


class User(UUIDPk, TimestampMixin, Base):
    __tablename__ = "users"

    role: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # staff (master/seller) credentials — NULL for viewers
    username: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # viewer (google) identity — NULL for staff
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    google_sub: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(160))
    avatar_url: Mapped[str | None] = mapped_column(Text)

    # public, human-friendly identifier (SC-MST-0001 / SC-SLR-0001 / SC-CUS-000001)
    public_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    device_lock_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(64))

    created_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )

    master_account: Mapped["MasterAccount | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    seller_account: Mapped["SellerAccount | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    viewer_profile: Mapped["ViewerProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_users_role_active", "role", "is_active"),)

    # -- helpers ------------------------------------------------------------
    @property
    def is_master(self) -> bool:
        return self.role == Role.MASTER

    @property
    def is_seller(self) -> bool:
        return self.role == Role.SELLER

    @property
    def is_viewer(self) -> bool:
        return self.role == Role.VIEWER

    @property
    def label(self) -> str:
        return self.display_name or self.username or self.email or self.public_code


class MasterAccount(UUIDPk, TimestampMixin, Base):
    __tablename__ = "master_accounts"

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    is_root: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_manage_masters: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="master_account")


class SellerAccount(UUIDPk, TimestampMixin, Base):
    __tablename__ = "seller_accounts"

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    seller_code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    can_verify_payments: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="seller_account")


class ViewerProfile(UUIDPk, TimestampMixin, Base):
    __tablename__ = "viewer_profiles"

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    customer_code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    google_email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    google_name: Mapped[str | None] = mapped_column(String(160))
    picture_url: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str | None] = mapped_column(String(16))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="viewer_profile")
