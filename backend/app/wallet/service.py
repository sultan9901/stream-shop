"""Coin wallet ledger — the single source of truth for every coin movement.

Invariants enforced here
------------------------
1. **Ledger first.** No balance changes without a ``wallet_transactions`` row.
2. **Idempotency.** Each movement carries a unique ``idempotency_key``; a replay
   returns the original transaction instead of moving coins again.
3. **Row locking.** The wallet row is locked (``SELECT ... FOR UPDATE`` on
   PostgreSQL) before the balance is read, so concurrent requests serialise.
4. **Server side only.** Nothing here trusts a client-supplied balance.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.codes import next_code
from app.config import settings
from app.models.base import TxnType
from app.models.wallet import Wallet, WalletTransaction


class InsufficientCoins(Exception):
    def __init__(self, required: int, available: int) -> None:
        self.required = int(required)
        self.available = int(available)
        self.shortfall = max(self.required - self.available, 0)
        super().__init__(f"need {self.required} coins, wallet holds {self.available}")


class WalletFrozen(Exception):
    pass


@dataclass(slots=True)
class Movement:
    transaction: WalletTransaction
    balance: int
    duplicate: bool = False


async def get_or_create_wallet(db: AsyncSession, user_id: str) -> Wallet:
    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == user_id))
    ).scalar_one_or_none()
    if wallet is not None:
        return wallet
    wallet = Wallet(user_id=user_id, balance=0)
    db.add(wallet)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:  # concurrent creation
        db.expire_all()
        wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user_id))).scalar_one()
    return wallet


async def lock_wallet(db: AsyncSession, user_id: str) -> Wallet:
    """Fetch the wallet with a write lock (no-op lock on SQLite, which serialises
    writers anyway). Always call this before reading a balance for a mutation."""
    await get_or_create_wallet(db, user_id)
    stmt = select(Wallet).where(Wallet.user_id == user_id)
    if not settings.is_sqlite:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one()


async def balance_of(db: AsyncSession, user_id: str) -> int:
    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == user_id))
    ).scalar_one_or_none()
    return int(wallet.balance) if wallet else 0


async def find_by_idempotency(db: AsyncSession, key: str) -> WalletTransaction | None:
    return (
        await db.execute(
            select(WalletTransaction).where(WalletTransaction.idempotency_key == key)
        )
    ).scalar_one_or_none()


async def _apply(
    db: AsyncSession,
    *,
    user_id: str,
    amount: int,
    txn_type: str,
    idempotency_key: str,
    reason: str | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    bdt_amount: float | None = None,
    payment_method: str | None = None,
    performed_by_id: str | None = None,
    performed_by_label: str | None = None,
    allow_negative: bool = False,
) -> Movement:
    existing = await find_by_idempotency(db, idempotency_key)
    if existing is not None:
        return Movement(transaction=existing, balance=int(existing.balance_after), duplicate=True)

    wallet = await lock_wallet(db, user_id)
    if wallet.is_frozen:
        raise WalletFrozen("This wallet is frozen. Contact support.")

    current = int(wallet.balance)
    if amount < 0 and not allow_negative and current + amount < 0:
        raise InsufficientCoins(required=-amount, available=current)

    new_balance = current + int(amount)
    wallet.balance = new_balance
    wallet.version = int(wallet.version) + 1
    if amount > 0:
        wallet.lifetime_credited = int(wallet.lifetime_credited) + int(amount)
    else:
        wallet.lifetime_spent = int(wallet.lifetime_spent) - int(amount)

    txn = WalletTransaction(
        reference_code=await next_code(db, "txn"),
        wallet_id=wallet.id,
        user_id=user_id,
        txn_type=txn_type,
        amount=int(amount),
        balance_after=new_balance,
        bdt_amount=bdt_amount,
        payment_method=payment_method,
        reference_type=reference_type,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        reason=reason,
        performed_by_id=performed_by_id,
        performed_by_label=performed_by_label,
    )
    db.add(txn)
    try:
        # SAVEPOINT: an idempotency-key collision must not tear down the caller's
        # surrounding transaction (order creation, payment state change, ...).
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        # On SQLite in particular, a failed flush inside a SAVEPOINT can leave the
        # *outer* session transaction marked "must rollback" even after the
        # SAVEPOINT itself unwinds cleanly -- any further use of `db` (including
        # the recovery lookup below) then raises PendingRollbackError instead of
        # running. That is recoverable: nothing else was written in this request
        # before this point, so a full rollback here only discards the losing
        # side of the idempotency race, never a caller's already-applied work.
        db.expire_all()
        try:
            existing = await find_by_idempotency(db, idempotency_key)
        except PendingRollbackError:
            await db.rollback()
            existing = await find_by_idempotency(db, idempotency_key)
        if existing is None:
            raise
        return Movement(transaction=existing, balance=int(existing.balance_after), duplicate=True)

    return Movement(transaction=txn, balance=new_balance)


# --------------------------------------------------------------------------
# public operations
# --------------------------------------------------------------------------
async def credit(db: AsyncSession, *, user_id: str, coins: int, **kw) -> Movement:
    if coins <= 0:
        raise ValueError("credit amount must be positive")
    kw.setdefault("txn_type", TxnType.COIN_PURCHASE)
    return await _apply(db, user_id=user_id, amount=int(coins), **kw)


async def debit(db: AsyncSession, *, user_id: str, coins: int, **kw) -> Movement:
    if coins <= 0:
        raise ValueError("debit amount must be positive")
    kw.setdefault("txn_type", TxnType.COIN_SPENT)
    return await _apply(db, user_id=user_id, amount=-int(coins), **kw)


async def history(
    db: AsyncSession, *, user_id: str, limit: int = 50, offset: int = 0
) -> list[WalletTransaction]:
    stmt = (
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user_id)
        .order_by(WalletTransaction.created_at.desc(), WalletTransaction.reference_code.desc())
        .limit(min(limit, 200))
        .offset(max(offset, 0))
    )
    return list((await db.execute(stmt)).scalars())


def serialise(txn: WalletTransaction) -> dict:
    return {
        "id": txn.id,
        "reference_code": txn.reference_code,
        "type": txn.txn_type,
        "amount": int(txn.amount),
        "balance_after": int(txn.balance_after),
        "bdt_amount": float(txn.bdt_amount) if txn.bdt_amount is not None else None,
        "payment_method": txn.payment_method,
        "status": txn.status,
        "reason": txn.reason,
        "approved_by": txn.performed_by_label,
        "reference_type": txn.reference_type,
        "reference_id": txn.reference_id,
        "created_at": txn.created_at.isoformat() if txn.created_at else None,
    }


async def audit_consistency(db: AsyncSession, user_id: str) -> dict:
    """Compare one wallet's cached balance against its ledger sum (admin view + tests)."""
    from sqlalchemy import func

    wallet = await get_or_create_wallet(db, user_id)
    total = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(WalletTransaction.amount), 0)).where(
                    WalletTransaction.user_id == user_id
                )
            )
        ).scalar()
        or 0
    )
    return {
        "user_id": user_id,
        "cached_balance": int(wallet.balance),
        "ledger_sum": total,
        "consistent": int(wallet.balance) == total,
    }


async def audit_consistency_all(db: AsyncSession, sample: int = 5) -> dict:
    """Site-wide ledger integrity, in one grouped aggregate (used by ``/health``).

    ``wallets.balance`` is only ever a cache of ``SUM(wallet_transactions.amount)``.
    A single ``HAVING`` returns *only* the wallets where the two disagree, so the
    healthy answer costs one scan and carries no rows. Any drift means a balance
    was written outside the ledger path and must be investigated immediately.
    """
    from sqlalchemy import func

    ledger_sum = func.coalesce(func.sum(WalletTransaction.amount), 0)
    rows = (
        await db.execute(
            select(Wallet.user_id, Wallet.balance, ledger_sum.label("ledger_sum"))
            .outerjoin(WalletTransaction, WalletTransaction.user_id == Wallet.user_id)
            .group_by(Wallet.user_id, Wallet.balance)
            .having(Wallet.balance != ledger_sum)
        )
    ).all()
    return {
        "consistent": not rows,
        "drifted_wallets": len(rows),
        "sample": [
            {"user_id": r.user_id, "cached_balance": int(r.balance), "ledger_sum": int(r.ledger_sum)}
            for r in rows[:sample]
        ],
    }

