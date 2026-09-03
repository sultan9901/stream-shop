"""Device binding is server-side, not IP-based. A device-locked staff account
bound to one device is refused from any other, with the exact spec message, until
a Master resets the binding (§5, §6, §9)."""
from __future__ import annotations

from app.auth.device import BOUND_TO_OTHER_DEVICE
from conftest import staff_login


async def test_second_device_is_refused(new_seller, clients):
    seller = await new_seller(device_lock=True)  # already logged in on device A

    # a brand-new browser (fresh cookie jar) with a different device id
    res = await staff_login(
        clients(), "seller", seller.username, seller.password, device_id="other-device-XYZ"
    )
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == BOUND_TO_OTHER_DEVICE


async def test_same_device_can_relogin(new_seller, clients):
    seller = await new_seller(device_lock=True)
    # same device id, new browser session — allowed
    again = await staff_login(
        clients(), "seller", seller.username, seller.password, device_id=seller.device_id
    )
    assert again.status_code == 200, again.text


async def test_master_reset_device_unbinds(new_seller, clients, master):
    seller = await new_seller(device_lock=True)

    reset = await master.post(f"/api/master/accounts/{seller.id}/reset-device")
    assert reset.status_code == 200, reset.text

    # now a different device may bind afresh
    res = await staff_login(
        clients(), "seller", seller.username, seller.password, device_id="fresh-device-after-reset"
    )
    assert res.status_code == 200, res.text


async def test_device_lock_disabled_allows_any_device(new_seller, clients):
    seller = await new_seller(device_lock=False)
    res = await staff_login(
        clients(), "seller", seller.username, seller.password, device_id="any-other-device"
    )
    assert res.status_code == 200, res.text
