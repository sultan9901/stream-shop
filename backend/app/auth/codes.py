"""Human-readable sequential codes (SC-ORD-000001) backed by a DB counter."""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Counter

PREFIXES = {
    "order": ("SC-ORD", 6),
    "payment": ("SC-PAY", 6),
    "txn": ("SC-TXN", 8),
    "customer": ("SC-CUS", 6),
    "seller": ("SC-SLR", 4),
    "master": ("SC-MST", 4),
}


async def next_code(db: AsyncSession, kind: str) -> str:
    """Atomically increment ``kind``'s counter and format the code.

    Uses a conditional UPDATE loop so two concurrent callers can never receive
    the same number, on both PostgreSQL and SQLite.
    """
    prefix, width = PREFIXES.get(kind, ("SC-GEN", 6))

    row = (await db.execute(select(Counter).where(Counter.name == kind))).scalar_one_or_none()
    if row is None:
        db.add(Counter(name=kind, value=0))
        await db.flush()
        row = (await db.execute(select(Counter).where(Counter.name == kind))).scalar_one()

    for _ in range(25):
        current = row.value
        res = await db.execute(
            update(Counter)
            .where(Counter.name == kind, Counter.value == current)
            .values(value=current + 1)
        )
        if res.rowcount:
            return f"{prefix}-{current + 1:0{width}d}"
        await db.refresh(row)
    raise RuntimeError(f"could not allocate code for {kind}")


def slugify(text: str, max_len: int = 200) -> str:
    out: list[str] = []
    prev_dash = False
    for ch in (text or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")[:max_len]
    return slug or "item"
