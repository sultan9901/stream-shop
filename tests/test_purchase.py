"""Coin deduction, the duplicate-purchase guard and the insufficient-coins path
are all server-side and transactional. A double-click (same idempotency key,
even fired concurrently) must deduct coins once and yield one order (§28–§31)."""
from __future__ import annotations

import asyncio
from uuid import uuid4

from app.wallet import service as wallet_service


async def test_happy_path_purchase(new_viewer, grant_coins, product, db):
    v = await new_viewer()
    price = product["coin_price"]
    await grant_coins(v.id, price + 50)

    res = await v.post(
        "/api/orders/purchase",
        json={"product_id": product["id"], "idempotency_key": uuid4().hex},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["order"]["status"] == "PAID"
    assert body["order"]["order_code"].startswith("SC-ORD-")
    assert body["balance"] == 50

    audit = await wallet_service.audit_consistency(db, v.id)
    assert audit["consistent"] is True
    assert audit["ledger_sum"] == 50


async def test_insufficient_coins_blocks_and_reports_shortfall(new_viewer, grant_coins, new_product):
    v = await new_viewer()
    product = await new_product(coin_price=200)
    await grant_coins(v.id, 120)

    res = await v.post(
        "/api/orders/purchase",
        json={"product_id": product["id"], "idempotency_key": uuid4().hex},
    )
    assert res.status_code == 402, res.text
    detail = res.json()["detail"]
    assert detail["error"] == "insufficient_coins"
    assert detail["required"] == 200
    assert detail["balance"] == 120
    assert detail["shortfall"] == 80
    # coins were NOT touched
    assert (await v.get("/api/wallet")).json()["balance"] == 120


async def test_same_idempotency_key_deducts_once(new_viewer, grant_coins, new_product):
    v = await new_viewer()
    product = await new_product(coin_price=100)
    await grant_coins(v.id, 300)
    key = uuid4().hex

    first = await v.post("/api/orders/purchase",
                         json={"product_id": product["id"], "idempotency_key": key})
    second = await v.post("/api/orders/purchase",
                          json={"product_id": product["id"], "idempotency_key": key})
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    # the replay returns the same order and does not deduct again
    assert first.json()["order"]["order_code"] == second.json()["order"]["order_code"]
    assert (await v.get("/api/wallet")).json()["balance"] == 200


async def test_concurrent_double_click_deducts_once(new_viewer, grant_coins, new_product):
    v = await new_viewer()
    product = await new_product(coin_price=100)
    await grant_coins(v.id, 300)
    key = uuid4().hex

    r1, r2 = await asyncio.gather(
        v.post("/api/orders/purchase", json={"product_id": product["id"], "idempotency_key": key}),
        v.post("/api/orders/purchase", json={"product_id": product["id"], "idempotency_key": key}),
    )
    assert {r1.status_code, r2.status_code} == {201}
    codes = {r1.json()["order"]["order_code"], r2.json()["order"]["order_code"]}
    assert len(codes) == 1  # one order, not two
    assert (await v.get("/api/wallet")).json()["balance"] == 200


async def test_repurchase_of_owned_product_is_blocked(new_viewer, grant_coins, new_product):
    v = await new_viewer()
    product = await new_product(coin_price=100)  # allow_repurchase defaults False
    await grant_coins(v.id, 300)

    ok = await v.post("/api/orders/purchase",
                      json={"product_id": product["id"], "idempotency_key": uuid4().hex})
    assert ok.status_code == 201, ok.text

    again = await v.post("/api/orders/purchase",
                         json={"product_id": product["id"], "idempotency_key": uuid4().hex})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["error"] == "already_owned"
    # still only charged once
    assert (await v.get("/api/wallet")).json()["balance"] == 200


async def test_affordability_block_tracks_balance(new_viewer, grant_coins, new_product):
    v = await new_viewer()
    product = await new_product(coin_price=150)
    await grant_coins(v.id, 60)

    detail = (await v.get(f"/api/products/{product['id']}")).json()
    aff = detail["affordability"]
    assert aff["authenticated"] is True
    assert aff["balance"] == 60
    assert aff["required"] == 150
    assert aff["shortfall"] == 90
    assert aff["can_afford"] is False
