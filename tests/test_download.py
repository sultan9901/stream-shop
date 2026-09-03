"""Secure downloads (§32): there is no public permanent URL. Every download runs
through a per-order token that is hashed at rest, expires, has a hit ceiling, is
owner-bound for signed-in users, and 404s when unknown. Payment screenshots are
staff-only."""
from __future__ import annotations

from uuid import uuid4


async def _buy_and_link(viewer, grant_coins, product):
    await grant_coins(viewer.id, product["coin_price"])
    order = (await viewer.post(
        "/api/orders/purchase",
        json={"product_id": product["id"], "idempotency_key": uuid4().hex},
    )).json()["order"]
    link = await viewer.post(f"/api/orders/{order['id']}/download-link")
    assert link.status_code == 200, link.text
    return order, link.json()


async def test_owner_can_download_the_file(new_viewer, grant_coins, product):
    v = await new_viewer()
    _, link = await _buy_and_link(v, grant_coins, product)
    assert link["max_downloads"] == 3  # DOWNLOAD_MAX_ATTEMPTS in the test env

    res = await v.client.get(link["download_url"])
    assert res.status_code == 200, res.text
    # it really is the stored zip we attached in the fixture
    assert res.content[:2] == b"PK"
    assert res.headers.get("cache-control") == "no-store"


async def test_unknown_token_is_404(anon):
    res = await anon.get("/download/this-token-does-not-exist")
    assert res.status_code == 404, res.text


async def test_download_ceiling_is_enforced(new_viewer, grant_coins, product):
    v = await new_viewer()
    _, link = await _buy_and_link(v, grant_coins, product)
    url = link["download_url"]

    for i in range(3):  # the ceiling
        ok = await v.client.get(url)
        assert ok.status_code == 200, f"download {i} failed: {ok.text}"

    exhausted = await v.client.get(url)
    assert exhausted.status_code == 429, exhausted.text


async def test_token_is_owner_bound(new_viewer, grant_coins, product):
    owner = await new_viewer()
    _, link = await _buy_and_link(owner, grant_coins, product)

    intruder = await new_viewer()
    res = await intruder.client.get(link["download_url"])
    assert res.status_code == 403, res.text


async def test_anonymous_visitor_may_use_the_emailed_link(new_viewer, grant_coins, product, anon):
    v = await new_viewer()
    _, link = await _buy_and_link(v, grant_coins, product)
    # the token was mailed to the customer; an unauthenticated request is allowed
    res = await anon.get(link["download_url"])
    assert res.status_code == 200, res.text


async def test_payment_screenshots_are_staff_only(viewer):
    res = await viewer.get("/api/payments/screenshot/whatever-id")
    assert res.status_code == 401, res.text
