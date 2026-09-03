"""Master-editable site settings (typed key/value store)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Setting

DEFAULTS: dict[str, tuple[str, str, str, str]] = {
    # key: (value, type, group, label)
    "site.tagline": ("Premium Coin-Based Software Marketplace", "str", "branding", "Site tagline"),
    "site.support_email": ("support@streamcorporation.local", "str", "branding", "Support email"),
    "site.support_whatsapp": ("", "str", "branding", "Support WhatsApp / phone"),
    "site.announcement": ("", "str", "branding", "Announcement banner"),
    "site.seo_description": (
        "STREAM CORPORATION — buy premium software with coins. Instant Gmail delivery, secure downloads.",
        "str", "seo", "SEO description",
    ),
    "site.seo_keywords": (
        "software marketplace, premium software, coin wallet, stream corporation",
        "str", "seo", "SEO keywords",
    ),
    "payment.instructions": (
        "Send the exact amount to the payment number above.\n"
        "After payment, upload your payment screenshot below.",
        "str", "payments", "Payment instructions",
    ),
    "payment.min_screenshot_note": (
        "Screenshots must clearly show the amount, the receiver number and the transaction ID.",
        "str", "payments", "Screenshot guidance",
    ),
    "intro.enabled": ("true", "bool", "ui", "Show intro splash"),
    "intro.duration_ms": ("3200", "int", "ui", "Intro duration (ms)"),
}


def _cast(value: str | None, value_type: str):
    if value is None:
        return None
    if value_type == "int":
        try:
            return int(value)
        except ValueError:
            return 0
    if value_type == "bool":
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if value_type == "float":
        try:
            return float(value)
        except ValueError:
            return 0.0
    return value


async def ensure_defaults(db: AsyncSession) -> None:
    existing = {
        row.key for row in (await db.execute(select(Setting))).scalars()
    }
    for key, (value, vtype, group, label) in DEFAULTS.items():
        if key not in existing:
            db.add(Setting(key=key, value=value, value_type=vtype, group=group, label=label))
    await db.flush()


async def all_settings(db: AsyncSession) -> dict:
    rows = list((await db.execute(select(Setting).order_by(Setting.group, Setting.key))).scalars())
    out: dict = {}
    for r in rows:
        out[r.key] = _cast(r.value, r.value_type)
    for key, (value, vtype, _g, _l) in DEFAULTS.items():
        out.setdefault(key, _cast(value, vtype))
    return out


async def detailed(db: AsyncSession) -> list[dict]:
    rows = list((await db.execute(select(Setting).order_by(Setting.group, Setting.key))).scalars())
    return [
        {
            "key": r.key,
            "value": r.value,
            "typed_value": _cast(r.value, r.value_type),
            "type": r.value_type,
            "group": r.group,
            "label": r.label,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


async def get(db: AsyncSession, key: str, default=None):
    row = (await db.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    if row is None:
        if key in DEFAULTS:
            value, vtype, _g, _l = DEFAULTS[key]
            return _cast(value, vtype)
        return default
    return _cast(row.value, row.value_type)


async def set_many(db: AsyncSession, values: dict[str, str], *, actor_id: str | None = None) -> int:
    changed = 0
    for key, raw in values.items():
        row = (await db.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
        if row is None:
            spec = DEFAULTS.get(key)
            row = Setting(
                key=key,
                value_type=spec[1] if spec else "str",
                group=spec[2] if spec else "general",
                label=spec[3] if spec else key,
            )
            db.add(row)
        row.value = "" if raw is None else str(raw)
        row.updated_by_id = actor_id
        changed += 1
    await db.flush()
    return changed
