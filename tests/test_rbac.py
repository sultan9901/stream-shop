"""Server-side authorisation is never trusted to the frontend. A viewer cannot
reach any Master/Seller API; a Seller cannot reach Master-only APIs; a Seller
without the payment permission cannot review payments; and every unsafe request
must carry a valid CSRF token (§45, §47, the CRITICAL RULE)."""
from __future__ import annotations

from uuid import uuid4


# ---- viewers are walled off from the staff APIs (401, no staff session) ----
async def test_viewer_cannot_list_master_orders(viewer):
    res = await viewer.get("/api/master/orders")
    assert res.status_code == 401, res.text


async def test_viewer_cannot_list_sellers(viewer):
    res = await viewer.get("/api/master/sellers")
    assert res.status_code == 401, res.text


async def test_viewer_cannot_reach_seller_console(viewer):
    res = await viewer.get("/api/seller/overview")
    assert res.status_code == 401, res.text


async def test_viewer_cannot_adjust_a_wallet(viewer, new_viewer):
    victim = await new_viewer()
    res = await viewer.post(
        f"/api/master/customers/{victim.id}/wallet",
        json={"coins": 1000, "direction": "add", "reason": "self service hack"},
    )
    assert res.status_code == 401, res.text


# ---- sellers cannot cross into Master-only territory (403, wrong role) ----
async def test_seller_cannot_reach_master_api(new_seller):
    seller = await new_seller()
    res = await seller.actor.get("/api/master/orders")
    assert res.status_code == 403, res.text


async def test_master_is_not_a_seller(master):
    res = await master.get("/api/seller/overview")
    assert res.status_code == 403, res.text


# ---- fine-grained seller permission: payment verification ----
async def test_seller_without_permission_cannot_review_payments(new_seller):
    seller = await new_seller(can_verify_payments=False)
    res = await seller.actor.get("/api/seller/payments")
    assert res.status_code == 403, res.text


async def test_seller_with_permission_can_list_payments(new_seller):
    seller = await new_seller(can_verify_payments=True)
    res = await seller.actor.get("/api/seller/payments")
    assert res.status_code == 200, res.text


# ---- CSRF: an unsafe request without the token is rejected even when authed ----
async def test_unsafe_request_without_csrf_is_blocked(new_viewer):
    v = await new_viewer()
    # bypass the Actor wrapper: hit the raw client so no X-CSRF-Token is sent,
    # even though the session cookie IS present.
    res = await v.client.post(
        "/api/orders/purchase",
        json={"product_id": "does-not-matter", "idempotency_key": uuid4().hex},
    )
    assert res.status_code == 403, res.text
    assert "CSRF" in res.json()["detail"]


async def test_csrf_present_passes_the_guard(new_viewer, grant_coins, product):
    v = await new_viewer()
    await grant_coins(v.id, product["coin_price"])
    # the Actor DOES echo the token — same request now clears CSRF and succeeds
    res = await v.post(
        "/api/orders/purchase",
        json={"product_id": product["id"], "idempotency_key": uuid4().hex},
    )
    assert res.status_code == 201, res.text
