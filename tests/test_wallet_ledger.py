"""The wallet is ledger-backed: the cached balance must always equal the sum of
every ``wallet_transactions`` row, and no coin can move without a ledger entry
(§41, §49). These tests drive the *real* master adjustment endpoint rather than
writing balances directly, so the ledger, the audit row and the balance
recomputation all have to line up."""
from __future__ import annotations

from app.wallet import service as wallet_service


async def test_grant_is_ledgered_and_consistent(new_viewer, grant_coins, db):
    v = await new_viewer()
    await grant_coins(v.id, 500, reason="first top-up")

    wallet = await v.get("/api/wallet")
    assert wallet.status_code == 200, wallet.text
    body = wallet.json()
    assert body["balance"] == 500
    assert body["lifetime_credited"] == 500
    assert body["lifetime_spent"] == 0

    # the ledger is the source of truth, and it agrees with the cached balance
    audit = await wallet_service.audit_consistency(db, v.id)
    assert audit["consistent"] is True
    assert audit["ledger_sum"] == 500
    assert audit["cached_balance"] == 500


async def test_multiple_movements_accumulate(new_viewer, grant_coins, master, db):
    v = await new_viewer()
    await grant_coins(v.id, 500)
    await grant_coins(v.id, 250, reason="second top-up")

    remove = await master.post(
        f"/api/master/customers/{v.id}/wallet",
        json={"coins": 100, "direction": "remove", "reason": "manual correction"},
    )
    assert remove.status_code == 200, remove.text
    assert remove.json()["balance"] == 650

    txns = (await v.get("/api/wallet/transactions")).json()["transactions"]
    # 2 credits + 1 debit, newest first
    assert len(txns) == 3
    assert txns[0]["amount"] == -100
    assert txns[0]["type"] == "ADMIN_DEBIT"
    assert {t["amount"] for t in txns} == {500, 250, -100}
    # every row carries the mandatory reason
    assert all(t["reason"] for t in txns)
    # running balance_after is coherent on the newest row
    assert txns[0]["balance_after"] == 650

    audit = await wallet_service.audit_consistency(db, v.id)
    assert audit["consistent"] is True
    assert audit["ledger_sum"] == 650


async def test_remove_more_than_balance_is_refused(new_viewer, grant_coins, master, db):
    v = await new_viewer()
    await grant_coins(v.id, 40)

    res = await master.post(
        f"/api/master/customers/{v.id}/wallet",
        json={"coins": 100, "direction": "remove", "reason": "overdraw attempt"},
    )
    assert res.status_code == 400, res.text
    assert res.json()["detail"]["error"] == "insufficient_coins"

    # nothing changed — no phantom debit landed in the ledger
    audit = await wallet_service.audit_consistency(db, v.id)
    assert audit["consistent"] is True
    assert audit["ledger_sum"] == 40


async def test_reason_is_mandatory(new_viewer, master):
    v = await new_viewer()
    res = await master.post(
        f"/api/master/customers/{v.id}/wallet",
        json={"coins": 10, "direction": "add", "reason": "x"},  # min_length=3
    )
    assert res.status_code == 422, res.text


async def test_bonus_direction_credits_as_bonus(new_viewer, grant_coins, master):
    v = await new_viewer()
    res = await master.post(
        f"/api/master/customers/{v.id}/wallet",
        json={"coins": 75, "direction": "bonus", "reason": "welcome bonus"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["transaction"]["type"] == "BONUS_COIN"


async def test_health_reports_site_wide_ledger_integrity(anon, new_viewer, grant_coins):
    """``/health`` is the operational probe for the ledger invariant, so it must
    actually re-derive the sum rather than trust ``wallets.balance``."""
    v = await new_viewer()
    await grant_coins(v.id, 300, reason="health probe top-up")

    res = await anon.get("/health")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["database"] == "up"
    assert body["wallet_ledger"] == {"checked": True, "consistent": True, "drifted_wallets": 0, "sample": []}


async def test_health_flags_a_balance_written_outside_the_ledger(anon, new_viewer, grant_coins, db):
    """Simulate the one thing the invariant forbids — a cached balance edited without a
    ledger row — and prove the probe turns red instead of silently accepting it."""
    from sqlalchemy import update

    from app.models.wallet import Wallet

    v = await new_viewer()
    await grant_coins(v.id, 300)

    await db.execute(update(Wallet).where(Wallet.user_id == v.id).values(balance=999_999))
    await db.commit()
    try:
        body = (await anon.get("/health")).json()
        assert body["ok"] is False
        assert body["wallet_ledger"]["consistent"] is False
        assert body["wallet_ledger"]["drifted_wallets"] == 1
        assert body["wallet_ledger"]["sample"][0] == {
            "user_id": v.id,
            "cached_balance": 999_999,
            "ledger_sum": 300,
        }
    finally:  # leave the fixture database consistent for any later test
        await db.execute(update(Wallet).where(Wallet.user_id == v.id).values(balance=300))
        await db.commit()

    assert (await anon.get("/health")).json()["ok"] is True

