"""Refund returns coins to the ledger, flips the order to REFUNDED and revokes the
download grant. It is idempotent — a second refund adds nothing (§34)."""
from __future__ import annotations

from uuid import uuid4

from app.wallet import service as wallet_service


async def _buy(viewer, grant_coins, product):
    price = product["coin_price"]
    await grant_coins(viewer.id, price)
    res = await viewer.post(
        "/api/orders/purchase",
        json={"product_id": product["id"], "idempotency_key": uuid4().hex},
    )
    assert res.status_code == 201, res.text
    return res.json()["order"]


async def test_refund_returns_coins_and_flips_status(new_viewer, grant_coins, product, master, db):
    v = await new_viewer()
    order = await _buy(v, grant_coins, product)
    assert (await v.get("/api/wallet")).json()["balance"] == 0

    res = await master.post(
        f"/api/master/orders/{order['id']}/refund", json={"reason": "customer changed mind"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["order"]["status"] == "REFUNDED"
    assert res.json()["balance"] == product["coin_price"]

    # coins are back, and the ledger still balances
    assert (await v.get("/api/wallet")).json()["balance"] == product["coin_price"]
    audit = await wallet_service.audit_consistency(db, v.id)
    assert audit["consistent"] is True
    assert audit["ledger_sum"] == product["coin_price"]

    # a COIN_REFUND row exists
    types = {t["type"] for t in (await v.get("/api/wallet/transactions")).json()["transactions"]}
    assert "COIN_REFUND" in types


async def test_refund_is_idempotent(new_viewer, grant_coins, product, master):
    v = await new_viewer()
    order = await _buy(v, grant_coins, product)

    first = await master.post(
        f"/api/master/orders/{order['id']}/refund", json={"reason": "duplicate"}
    )
    assert first.status_code == 200, first.text
    balance_after_first = (await v.get("/api/wallet")).json()["balance"]

    second = await master.post(
        f"/api/master/orders/{order['id']}/refund", json={"reason": "duplicate"}
    )
    assert second.status_code == 200, second.text
    # no double credit
    assert (await v.get("/api/wallet")).json()["balance"] == balance_after_first == product["coin_price"]


async def test_download_link_disabled_after_refund(new_viewer, grant_coins, product, master):
    v = await new_viewer()
    order = await _buy(v, grant_coins, product)

    # a fresh download grant works before refund
    pre = await v.post(f"/api/orders/{order['id']}/download-link")
    assert pre.status_code == 200, pre.text

    ref = await master.post(
        f"/api/master/orders/{order['id']}/refund", json={"reason": "revoke test"}
    )
    assert ref.status_code == 200, ref.text

    # issuing a new grant is now forbidden
    post = await v.post(f"/api/orders/{order['id']}/download-link")
    assert post.status_code == 403, post.text

    # and the previously-issued token is revoked
    dead = await v.client.get(pre.json()["download_url"])
    assert dead.status_code == 403, dead.text
