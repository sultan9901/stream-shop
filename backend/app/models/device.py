"""Device binding + server-side session records."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, TZDateTime, UUIDPk, utcnow
from app.models.user import User


class Device(UUIDPk, TimestampMixin, Base):
    """A device authorised for a staff account (master/seller).

    ``fingerprint_hash`` is an HMAC of the client-side device id, so the raw
    identifier is never stored. Binding is validated *server side* on every
    request via the session -> device link; IP is recorded for auditing only and
    is never used as the sole binding factor.
    """

    __tablename__ = "devices"

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(String(120))
    user_agent: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(String(80))
    first_ip: Mapped[str | None] = mapped_column(String(64))
    last_ip: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    bound_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    user: Mapped[User] = relationship()

    __table_args__ = (
        UniqueConstraint("user_id", "fingerprint_hash", name="uq_devices_user_fingerprint"),
        Index("ix_devices_user_active", "user_id", "is_active"),
    )


class Session(UUIDPk, TimestampMixin, Base):
    """Opaque server-side session. The cookie holds a random token; only its
    HMAC is persisted, so a database leak cannot be replayed as a login."""

    __tablename__ = "sessions"

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    surface: Mapped[str] = mapped_column(String(16), nullable=False)  # viewer | staff
    device_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("devices.id", ondelete="SET NULL")
    )
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    user: Mapped[User] = relationship()
    device: Mapped[Device | None] = relationship()

    __table_args__ = (Index("ix_sessions_user_surface", "user_id", "surface"),)


class LoginAttempt(UUIDPk, Base):
    """Login audit trail — successes and failures, for lockout + forensics."""

    __tablename__ = "login_attempts"

    identifier: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    surface: Mapped[str] = mapped_column(String(16), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(120))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, default=utcnow, nullable=False
    )
