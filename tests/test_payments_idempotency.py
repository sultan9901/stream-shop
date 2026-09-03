"""Uploading a screenshot NEVER credits coins — it only opens a PENDING request —
and confirming a payment credits coins *exactly once* no matter how many times
the button is pressed. The state transition is a conditional UPDATE guarded by an
idempotency key, so a double-confirm is a no-op (§25, §46, §48)."""
from __future__ import annotations

import asyncio


async def _open_request(viewer, png_bytes):
    """Viewer picks the first active package and submits proof."""
    packages = (await viewer.get("/api/wallet/packages")).json()
    pkg = packages["packages"][0]
    res = await viewer.post(
        "/api/wallet/payment-request",
        data={"package_id": pkg["id"], "sender_number": "01700000000",
              "transaction_ref": "TRX-TEST-1"},
        files={"screenshot": ("proof.png", png_bytes(), "image/png")},
    )
    assert res.status_code == 201, res.text
    return pkg, res.json()["request"]


async def test_upload_alone_credits_nothing(viewer, png_bytes):
    _, req = await _open_request(viewer, png_bytes)
    assert req["status"] == "PENDING"
    # the wallet is still empty — proof does not equal payment
    assert (await viewer.get("/api/wallet")).json()["balance"] == 0


async def test_confirm_credits_exactly_once(viewer, png_bytes, master):
    pkg, req = await _open_request(viewer, png_bytes)
    expected = pkg["coins"] + pkg["bonus_coins"]

    first = await master.post(f"/api/master/payments/{req['id']}/confirm")
    assert first.status_code == 200, first.text
    assert first.json().get("already_processed") is not True
    assert first.json()["coins_added"] == expected

    # a second confirm must not add coins again
    second = await master.post(f"/api/master/payments/{req['id']}/confirm")
    assert second.status_code == 200, second.text
    assert second.json()["already_processed"] is True

    assert (await viewer.get("/api/wallet")).json()["balance"] == expected


async def test_concurrent_confirm_is_still_once(viewer, png_bytes, master):
    pkg, req = await _open_request(viewer, png_bytes)
    expected = pkg["coins"] + pkg["bonus_coins"]

    # fire the confirm twice at once — only one may actually credit
    r1, r2 = await asyncio.gather(
        master.post(f"/api/master/payments/{req['id']}/confirm"),
        master.post(f"/api/master/payments/{req['id']}/confirm"),
    )
    assert {r1.status_code, r2.status_code} == {200}
    credited = [r for r in (r1.json(), r2.json()) if r.get("already_processed") is not True]
    assert len(credited) == 1
    assert credited[0]["coins_added"] == expected
    assert (await viewer.get("/api/wallet")).json()["balance"] == expected


async def test_reject_adds_no_coins(viewer, png_bytes, master):
    _, req = await _open_request(viewer, png_bytes)

    rej = await master.post(
        f"/api/master/payments/{req['id']}/reject", json={"reason": "blurry screenshot"}
    )
    assert rej.status_code == 200, rej.text
    assert (await viewer.get("/api/wallet")).json()["balance"] == 0

    # confirming an already-rejected request cannot resurrect it into a credit
    after = await master.post(f"/api/master/payments/{req['id']}/confirm")
    assert after.status_code == 200, after.text
    assert after.json()["already_processed"] is True
    assert (await viewer.get("/api/wallet")).json()["balance"] == 0
