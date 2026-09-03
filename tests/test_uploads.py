"""Upload hardening (§48): extension allow-list, magic-byte sniffing and a real
Pillow decode for images. A wrong extension, a lie about the content type, or a
corrupt image must all be refused; only a genuine image is stored."""
from __future__ import annotations


async def _package_id(viewer):
    packages = (await viewer.get("/api/wallet/packages")).json()
    return packages["packages"][0]["id"]


async def _submit_screenshot(viewer, filename, content, content_type):
    pkg = await _package_id(viewer)
    return await viewer.post(
        "/api/wallet/payment-request",
        data={"package_id": pkg},
        files={"screenshot": (filename, content, content_type)},
    )


async def test_wrong_extension_is_refused(viewer):
    res = await _submit_screenshot(viewer, "proof.txt", b"hello world", "text/plain")
    assert res.status_code == 400, res.text
    assert "Unsupported file type" in res.json()["detail"]


async def test_png_extension_but_not_an_image_is_refused(viewer):
    # right extension, wrong bytes — magic sniff must catch it
    res = await _submit_screenshot(viewer, "fake.png", b"this is definitely not a png", "image/png")
    assert res.status_code == 400, res.text


async def test_png_magic_but_corrupt_body_is_refused(viewer):
    # correct PNG signature so the sniff passes, but Pillow cannot decode it
    corrupt = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    res = await _submit_screenshot(viewer, "corrupt.png", corrupt, "image/png")
    assert res.status_code == 400, res.text


async def test_genuine_png_is_accepted(viewer, png_bytes):
    res = await _submit_screenshot(viewer, "real.png", png_bytes(), "image/png")
    assert res.status_code == 201, res.text
    assert res.json()["request"]["status"] == "PENDING"


async def test_empty_upload_is_refused(viewer):
    res = await _submit_screenshot(viewer, "empty.png", b"", "image/png")
    assert res.status_code == 400, res.text


async def test_product_file_rejects_disallowed_extension(master, product):
    # .txt is neither an archive nor an image type
    res = await master.post(
        f"/api/master/products/{product['id']}/file",
        files={"file": ("notes.txt", b"plain text payload", "text/plain")},
    )
    assert res.status_code == 400, res.text
    assert "Unsupported file type" in res.json()["detail"]
