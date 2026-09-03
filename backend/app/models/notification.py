"""Notifications / in-site chat messages, settings KV store, audit trail."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, TZDateTime, UUIDPk


class Notification(UUIDPk, TimestampMixin, Base):
    """A single message in the notification / chat centre.

    Targeting is either a specific user (``user_id``) or a whole audience
    (``audience`` = MASTER / SELLER), which is how "all masters" fan-out works
    without duplicating rows per admin.
    """

    __tablename__ = "notifications"

    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    audience: Mapped[str | None] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(24))
    link: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "is_read"),
        Index("ix_notifications_audience_created", "audience", "created_at"),
    )


class Setting(UUIDPk, TimestampMixin, Base):
    """Simple typed key/value store for site-wide configuration a Master edits."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    value: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(16), default="str", nullable=False)
    group: Mapped[str] = mapped_column(String(40), default="general", nullable=False)
    label: Mapped[str | None] = mapped_column(String(160))
    updated_by_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL")
    )


class AuditLog(UUIDPk, TimestampMixin, Base):
    __tablename__ = "audit_logs"

    actor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_label: Mapped[str | None] = mapped_column(String(160))
    actor_role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(40), index=True)
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    meta_json: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("ix_audit_action_created", "action", "created_at"),)


class Counter(UUIDPk, Base):
    """Atomic sequence source for human-readable codes (SC-ORD-000001, ...)."""

    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    value: Mapped[int] = mapped_column(default=0, nullable=False)
