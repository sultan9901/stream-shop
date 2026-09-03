"""Shared test fixtures for STREAM CORPORATION.

Two things here are load-bearing and easy to break:

1. **Environment before import.** ``app.config.settings`` is built at import time
   and ``app.config`` immediately creates the upload directories, while
   ``app.database`` creates the engine. So every setting this suite needs has to
   be in ``os.environ`` *before* the first ``import app.*`` (environment
   variables outrank the repo ``.env`` in pydantic-settings, so a developer's own
   ``.env`` cannot leak into a test run or, worse, get written to).
2. **One event loop.** ``app.database.engine`` is a module-level singleton bound
   to whichever loop first uses it, which is why ``pytest.ini`` pins both
   asyncio loop scopes to ``session``.

The suite talks to the real ASGI app over ``httpx.ASGITransport`` — real routing,
real dependencies, real middleware, real database. Nothing is mocked except the
Google identity provider, which the app itself exposes as a dev stub.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# --- scratch workspace: wiped at the start of every session -----------------
TMP = ROOT / "tests" / ".tmp"
shutil.rmtree(TMP, ignore_errors=True)
(TMP / "uploads").mkdir(parents=True, exist_ok=True)
DB_FILE = TMP / "stream_test.db"

MASTER_USERNAME = "Admin"
MASTER_PASSWORD = "admin"          # the spec-mandated default (§4)

os.environ.update(
    {
        "APP_NAME": "STREAM CORPORATION",
        "ENVIRONMENT": "test",
        "DEBUG": "false",
        "BASE_URL": "http://testserver",
        "SECRET_KEY": "test-only-secret-key-not-used-anywhere-else-0123456789",
        "ALLOWED_HOSTS": "*",
        "CORS_ORIGINS": "http://testserver",
        "DATABASE_URL": f"sqlite+aiosqlite:///{DB_FILE.as_posix()}",
        "REDIS_URL": "",
        "UPLOAD_DIR": str(TMP / "uploads"),
        "RATE_LIMIT_ENABLED": "false",
        "EMAIL_BACKEND": "console",
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "ALLOW_DEV_GOOGLE_STUB": "true",
        "DEFAULT_MASTER_USERNAME": MASTER_USERNAME,
        "DEFAULT_MASTER_PASSWORD": MASTER_PASSWORD,
        "COOKIE_SECURE": "false",
        "DOWNLOAD_TOKEN_TTL_HOURS": "72",
        "DOWNLOAD_MAX_ATTEMPTS": "3",
    }
)

# --- only now may the application be imported ------------------------------
import httpx  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402

BASE = "http://testserver"


# ==========================================================================
# lifespan / client plumbing
# ==========================================================================
@pytest.fixture(scope="session", autouse=True)
async def _boot():
    """Run the real startup: create_all + bootstrap seed + pubsub, then shut down."""
    async with app.router.lifespan_context(app):
        yield


@pytest.fixture
async def clients():
    """Factory for isolated browser-like clients (each with its own cookie jar)."""
    made: list[httpx.AsyncClient] = []

    def make() -> httpx.AsyncClient:
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE,
            follow_redirects=False,
        )
        made.append(c)
        return c

    yield make
    for c in made:
        await c.aclose()


@pytest.fixture
async def db():
    """A session for asserting directly against the database."""
    async with SessionLocal() as session:
        yield session


@pytest.fixture
async def anon(clients):
    return clients()

class Actor:
    """A logged-in browser: cookie jar + the CSRF token the server issued it.

    Unsafe verbs automatically echo ``X-CSRF-Token`` because that is what the real
    frontend does; tests that want to prove CSRF is enforced call the raw client
    instead (see ``test_rbac.py``).
    """

    def __init__(self, client: httpx.AsyncClient, *, csrf: str = "", user_id: str = "",
                 role: str = "", device_id: str | None = None, label: str = "") -> None:
        self.client = client
        self.csrf = csrf
        self.id = user_id
        self.role = role
        self.device_id = device_id
        self.label = label

    def _headers(self, extra: dict | None = None) -> dict:
        h = {}
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        if self.device_id:
            h["X-Device-Id"] = self.device_id
        if extra:
            h.update(extra)
        return h

    async def get(self, url: str, **kw) -> httpx.Response:
        kw["headers"] = self._headers(kw.pop("headers", None))
        return await self.client.get(url, **kw)

    async def post(self, url: str, **kw) -> httpx.Response:
        kw["headers"] = self._headers(kw.pop("headers", None))
        return await self.client.post(url, **kw)

    async def put(self, url: str, **kw) -> httpx.Response:
        kw["headers"] = self._headers(kw.pop("headers", None))
        return await self.client.put(url, **kw)

    async def patch(self, url: str, **kw) -> httpx.Response:
        kw["headers"] = self._headers(kw.pop("headers", None))
        return await self.client.patch(url, **kw)

    async def delete(self, url: str, **kw) -> httpx.Response:
        kw["headers"] = self._headers(kw.pop("headers", None))
        return await self.client.delete(url, **kw)

async def staff_login(
    client: httpx.AsyncClient, surface: str, username: str, password: str, device_id: str
) -> httpx.Response:
    return await client.post(
        f"/api/auth/{surface}/login",
        json={"username": username, "password": password, "device_id": device_id},
        headers={"X-Device-Id": device_id, "User-Agent": f"pytest/{device_id}"},
    )


async def as_staff(
    client: httpx.AsyncClient, surface: str, username: str, password: str, device_id: str
) -> Actor:
    res = await staff_login(client, surface, username, password, device_id)
    assert res.status_code == 200, res.text
    actor = Actor(client, csrf=res.json()["csrf_token"], role=res.json()["role"],
                  device_id=device_id, label=username)
    me = await actor.get("/api/auth/staff/me")
    assert me.status_code == 200, me.text
    actor.id = me.json()["user"]["id"]
    return actor


async def as_viewer(client: httpx.AsyncClient, email: str, name: str | None = None) -> Actor:
    """Sign a viewer in through the app's own dev Google stub."""
    res = await client.post(
        "/auth/google/dev-login", json={"email": email, "name": name or email.split("@")[0]}
    )
    assert res.status_code == 200, res.text
    actor = Actor(client, csrf=res.json()["csrf_token"], role="VIEWER", label=name or email)
    me = await actor.get("/api/auth/viewer/me")
    assert me.status_code == 200, me.text
    assert me.json()["authenticated"] is True
    actor.id = me.json()["user"]["id"]
    return actor

MASTER_DEVICE = "pytest-master-device-0001"


@pytest.fixture(scope="session")
async def master():
    """The bootstrap root master, kept for the whole session.

    Its own device stays bound the entire run — logging the same account in from
    a second device would (correctly) be refused by the device lock, which is
    exactly what ``test_device_lock.py`` asserts on purpose.
    """
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE, follow_redirects=False
    )
    actor = await as_staff(client, "master", MASTER_USERNAME, MASTER_PASSWORD, MASTER_DEVICE)
    yield actor
    await client.aclose()


@pytest.fixture
async def new_viewer(clients):
    async def make(email: str | None = None, name: str | None = None) -> Actor:
        return await as_viewer(clients(), email or f"buyer-{uuid4().hex[:10]}@gmail.com", name)

    return make


@pytest.fixture
async def viewer(new_viewer):
    return await new_viewer()


@dataclass
class SellerHandle:
    account: dict
    username: str
    password: str
    device_id: str
    actor: Actor | None = None

    @property
    def id(self) -> str:
        return self.account["id"]


@pytest.fixture
async def new_seller(master, clients):
    async def make(*, can_verify_payments: bool = True, device_lock: bool = True,
                   login: bool = True) -> SellerHandle:
        username = f"seller{uuid4().hex[:8]}"
        password = "seller-pass-123"
        res = await master.post(
            "/api/master/sellers",
            json={
                "username": username,
                "password": password,
                "contact_email": f"{username}@stream.local",
                "device_lock": device_lock,
                "can_verify_payments": can_verify_payments,
            },
        )
        assert res.status_code == 201, res.text
        handle = SellerHandle(
            account=res.json()["seller"], username=username, password=password,
            device_id=f"pytest-{username}",
        )
        if login:
            handle.actor = await as_staff(
                clients(), "seller", username, password, handle.device_id
            )
        return handle

    return make

@pytest.fixture(scope="session")
def png_bytes():
    """A genuine, decodable PNG.

    ``uploads.save_image`` sniffs magic bytes *and* runs ``PIL.Image.verify()``,
    so a hand-written stub with a PNG header would be rejected — which is what
    ``test_uploads.py`` proves separately.
    """
    from PIL import Image

    def make(size: tuple[int, int] = (48, 48), colour: str = "#00ffd5") -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", size, colour).save(buf, format="PNG")
        return buf.getvalue()

    return make


@pytest.fixture(scope="session")
def zip_bytes():
    """A real (tiny) zip archive to stand in for a deliverable."""
    import zipfile

    def make(name: str = "README.txt", body: bytes = b"STREAM CORPORATION test payload\n") -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(name, body)
        return buf.getvalue()

    return make


@pytest.fixture
def grant_coins(master):
    """Top a viewer's wallet up through the real master adjustment endpoint.

    Deliberately *not* a direct database write: the ledger, the audit row and the
    balance recomputation all have to happen for the wallet assertions to mean
    anything.
    """

    async def give(viewer_id: str, coins: int, reason: str = "pytest funding") -> dict:
        res = await master.post(
            f"/api/master/customers/{viewer_id}/wallet",
            json={"coins": coins, "direction": "add", "reason": reason},
        )
        assert res.status_code == 200, res.text
        return res.json()

    return give


@pytest.fixture
async def new_product(master, zip_bytes):
    """Create a sellable product, with a real attached file unless told otherwise."""

    async def make(*, coin_price: int = 100, name: str | None = None,
                   seller_id: str | None = None, with_file: bool = True,
                   is_active: bool = True, allow_repurchase: bool = False,
                   stock: int | None = None) -> dict:
        body = {
            "name": name or f"Test Suite App {uuid4().hex[:8]}",
            "coin_price": coin_price,
            "tagline": "Built by the test suite",
            "description": "A product created by pytest to exercise the real purchase flow.",
            "version": "1.0.0",
            "platform": "Windows",
            "is_active": is_active,
            "allow_repurchase": allow_repurchase,
            "stock": stock,
        }
        if seller_id:
            body["seller_id"] = seller_id
        res = await master.post("/api/master/products", json=body)
        assert res.status_code == 201, res.text
        product = res.json()["product"]
        if with_file:
            up = await master.post(
                f"/api/master/products/{product['id']}/file",
                files={"file": ("payload.zip", zip_bytes(), "application/zip")},
            )
            assert up.status_code == 201, up.text
        return product

    return make


@pytest.fixture
async def product(new_product):
    return await new_product()
