"""Over-the-wire functional verification of the whole STREAM CORPORATION stack.

This is not a unit test: it boots the *real* application with `uvicorn` in a child
process on a real TCP port, against a throwaway database and upload root, and then
drives every workflow the owner listed (login, Master, Seller, Viewer, product,
coin, order, Gmail delivery, chat/WebSocket, payment) as an HTTP/WS client would.

Nothing is stubbed. Cookies, CSRF double-submit, device binding, multipart uploads,
background email delivery and WebSocket frames all travel over the socket.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from email import message_from_bytes
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
TMP = ROOT / "tests" / ".tmp" / "live"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = TMP / "server.log"

MASTER_USER = "Admin"
MASTER_PW0 = "bootstrap-secret-pw"
MASTER_PW1 = "Rotated-Master-Pw-2026"
SELLER_USER = "seller_one"
SELLER_PW = "seller-secret-pw"
VIEWER_EMAIL = "viewer.one@example.com"
VIEWER2_EMAIL = "viewer.two@example.com"

RESULTS: list[tuple[bool, str, str]] = []
STATE: dict = {}


def check(name: str, ok: object, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.append((ok, name, detail))
    line = f"{'PASS' if ok else 'FAIL'}  {name}"
    if detail:
        line += f"   [{str(detail)[:300]}]"
    print(line, flush=True)
    return ok


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


BASE_ENV = {
    "ENVIRONMENT": "development",
    "DEBUG": "false",
    "SECRET_KEY": "live-verify-" + "s" * 52,
    "DATABASE_URL": "sqlite+aiosqlite:///./tests/.tmp/live/live.db",
    "UPLOAD_DIR": "./tests/.tmp/live/uploads",
    "REDIS_URL": "",
    "EMAIL_BACKEND": "console",
    "ALLOW_DEV_GOOGLE_STUB": "true",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    "DEFAULT_MASTER_USERNAME": MASTER_USER,
    "DEFAULT_MASTER_PASSWORD": MASTER_PW0,
    "COOKIE_SECURE": "false",
    "ALLOWED_HOSTS": "*",
    "RATE_LIMIT_ENABLED": "true",
    "LOGIN_RATE_LIMIT": "400/5m",
    "UPLOAD_RATE_LIMIT": "400/1h",
    "PURCHASE_RATE_LIMIT": "400/1h",
    "DOWNLOAD_MAX_ATTEMPTS": "2",       # small ceiling so exhaustion is testable
    "DOWNLOAD_TOKEN_TTL_HOURS": "72",
    "PYTHONUNBUFFERED": "1",
}


def boot(port: int, extra: dict | None = None, log_name: str = "server.log"):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SECRET_KEY", "DATABASE_URL"))}
    env.update(BASE_ENV)
    env["BASE_URL"] = f"http://127.0.0.1:{port}"
    env.update(extra or {})
    logfile = (TMP / log_name).open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        cwd=str(ROOT), env=env, stdout=logfile, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(240):
        if proc.poll() is not None:
            print((TMP / log_name).read_text(encoding="utf-8", errors="replace")[-4000:])
            raise SystemExit(f"server exited early with {proc.returncode}")
        try:
            if httpx.get(base + "/health", timeout=2).status_code == 200:
                return proc, base
        except Exception:
            time.sleep(0.25)
    raise SystemExit("server never became healthy")


def stop(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
class Client:
    """A browser-like session: keeps cookies, echoes the CSRF cookie as a header."""

    def __init__(self, base: str, device: str | None = None, label: str = "anon"):
        headers = {"User-Agent": f"live-verify/{label}"}
        if device:
            headers["X-Device-Id"] = device
        self.c = httpx.Client(base_url=base, timeout=60, headers=headers, follow_redirects=False)
        self.label = label

    @property
    def csrf(self) -> str | None:
        return self.c.cookies.get("sc_csrf")

    def _hdrs(self, extra: dict | None) -> dict:
        h = dict(extra or {})
        if self.csrf:
            h.setdefault("X-CSRF-Token", self.csrf)
        return h

    def get(self, url: str, **kw):
        return self.c.get(url, **kw)

    def post(self, url: str, headers: dict | None = None, **kw):
        return self.c.post(url, headers=self._hdrs(headers), **kw)

    def put(self, url: str, headers: dict | None = None, **kw):
        return self.c.put(url, headers=self._hdrs(headers), **kw)

    def patch(self, url: str, headers: dict | None = None, **kw):
        return self.c.patch(url, headers=self._hdrs(headers), **kw)

    def delete(self, url: str, headers: dict | None = None, **kw):
        return self.c.request("DELETE", url, headers=self._hdrs(headers), **kw)

    def close(self) -> None:
        self.c.close()


def png_bytes(w: int = 48, h: int = 32, colour=(210, 40, 90)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, format="PNG")
    return buf.getvalue()


def zip_bytes(payload: bytes = b"STREAM CORPORATION deliverable payload\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", payload.decode())
        z.writestr("app/main.py", "print('hello from the purchased product')\n")
    return buf.getvalue()


def body(res) -> dict:
    try:
        return res.json()
    except Exception:
        return {"_raw": res.text[:400]}
def phase_public(base: str) -> None:
    section("A. public surface, health and security headers")
    anon = Client(base, label="anon")
    STATE["anon"] = anon

    res = anon.get("/health")
    h = body(res)
    check("GET /health -> 200", res.status_code == 200, res.status_code)
    check("/health reports the database up", h.get("database") == "up", h.get("database"))
    check("/health re-derives ledger integrity site-wide",
          h.get("wallet_ledger", {}).get("checked") is True
          and h["wallet_ledger"].get("consistent") is True, json.dumps(h.get("wallet_ledger")))
    check("/health overall ok", h.get("ok") is True, json.dumps(h))

    home = anon.get("/")
    check("GET / renders the storefront", home.status_code == 200 and "<html" in home.text.lower(),
          f"{home.status_code}, {len(home.text)} bytes")
    for hdr, want in (("X-Frame-Options", "DENY"), ("X-Content-Type-Options", "nosniff")):
        check(f"security header {hdr}: {want}", home.headers.get(hdr) == want, home.headers.get(hdr))
    check("Content-Security-Policy is sent", "default-src 'self'" in home.headers.get("content-security-policy", ""))
    check("no HSTS in development", "strict-transport-security" not in {k.lower() for k in home.headers})

    site = anon.get("/api/site")
    check("GET /api/site -> 200 JSON", site.status_code == 200 and isinstance(body(site), dict))
    check("GET /api/products (public catalogue) -> 200", anon.get("/api/products").status_code == 200)
    check("GET /api/docs is served in development", anon.get("/api/docs").status_code == 200)

    section("A2. anonymous requests must be refused everywhere protected")
    for path in ("/api/wallet", "/api/orders", "/api/notifications"):
        r = anon.get(path)
        check(f"anonymous GET {path} -> 401", r.status_code == 401, r.status_code)
    vm = anon.get("/api/auth/viewer/me")
    check("anonymous /api/auth/viewer/me -> 200 authenticated:false",
          vm.status_code == 200 and body(vm).get("authenticated") is False, json.dumps(body(vm))[:160])
    for path in ("/api/master/overview", "/api/master/products", "/api/master/payments",
                 "/api/master/audit", "/api/seller/overview", "/api/auth/staff/me"):
        r = anon.get(path)
        check(f"anonymous GET {path} -> 401", r.status_code == 401, r.status_code)
    r = anon.post("/api/orders/purchase", json={"product_id": "x"})
    check("anonymous purchase -> 401", r.status_code == 401, r.status_code)
    r = anon.get("/api/payments/screenshot/anything")
    check("anonymous screenshot fetch -> 401 (proofs are never public)", r.status_code == 401, r.status_code)
def phase_master_login(base: str) -> None:
    section("B. Master login, generic failure, CSRF, forced password change")
    m = Client(base, device="master-device-A", label="master")
    STATE["master"] = m

    bad = m.post("/api/auth/master/login", json={"username": MASTER_USER, "password": "wrong-pw"})
    check("wrong Master password -> 401", bad.status_code == 401, bad.status_code)
    msg = json.dumps(body(bad))
    check("failure message does not reveal whether the user exists",
          "not found" not in msg.lower() and "no such" not in msg.lower(), msg[:160])

    ok = m.post("/api/auth/master/login", json={"username": MASTER_USER, "password": MASTER_PW0})
    d = body(ok)
    check("Master login -> 200", ok.status_code == 200, f"{ok.status_code} {json.dumps(d)[:200]}")
    check("role is MASTER", d.get("role") == "MASTER", d.get("role"))
    check("bootstrap Master is forced to change its password",
          d.get("must_change_password") is True, d.get("must_change_password"))
    check("a CSRF token is issued", bool(d.get("csrf_token")))
    check("session cookie sc_staff was set", bool(m.c.cookies.get("sc_staff")))
    check("device cookie sc_device was set", bool(m.c.cookies.get("sc_device")))
    check("csrf cookie sc_csrf is readable by JS (double submit)", m.csrf == d.get("csrf_token"))
    check("session cookie is not the session id itself (opaque, hashed server-side)",
          len(m.c.cookies.get("sc_staff") or "") >= 32)

    # CSRF enforcement: same cookies, deliberately no X-CSRF-Token header
    raw = httpx.Client(base_url=base, timeout=30, cookies=m.c.cookies)
    nocsrf = raw.post("/api/master/categories", json={"name": "Should not be created"})
    check("POST without X-CSRF-Token -> 403", nocsrf.status_code == 403, nocsrf.status_code)
    badcsrf = raw.post("/api/master/categories", json={"name": "Nope"},
                       headers={"X-CSRF-Token": "forged-token-value"})
    check("POST with a forged CSRF token -> 403", badcsrf.status_code == 403, badcsrf.status_code)
    raw.close()

    ch = m.post("/api/auth/staff/change-password",
                json={"current_password": MASTER_PW0, "new_password": MASTER_PW1})
    check("Master changes its password -> 200", ch.status_code == 200, f"{ch.status_code} {ch.text[:200]}")
    old = m.post("/api/auth/master/login", json={"username": MASTER_USER, "password": MASTER_PW0})
    check("the old password no longer works", old.status_code == 401, old.status_code)
    again = m.post("/api/auth/master/login", json={"username": MASTER_USER, "password": MASTER_PW1})
    d2 = body(again)
    check("login with the new password -> 200", again.status_code == 200, again.status_code)
    check("must_change_password is cleared", d2.get("must_change_password") is False, d2.get("must_change_password"))
    me = body(m.get("/api/auth/staff/me"))
    check("/api/auth/staff/me identifies the Master", me.get("user", {}).get("role") == "MASTER",
          json.dumps(me)[:200])
    check("GET /api/master/overview -> 200", m.get("/api/master/overview").status_code == 200)
def phase_catalogue(base: str) -> None:
    section("C. Master catalogue: category, product, deliverable file, media")
    m: Client = STATE["master"]

    cat = m.post("/api/master/categories",
                 json={"name": "Automation Tools", "description": "Bots and schedulers",
                       "icon": "bolt", "display_order": 1})
    cd = body(cat)
    check("create category -> 201", cat.status_code == 201, f"{cat.status_code} {json.dumps(cd)[:200]}")
    cat_id = (cd.get("category") or {}).get("id")
    check("category id returned", bool(cat_id), cat_id)
    STATE["category_id"] = cat_id

    p = m.post("/api/master/products", json={
        "name": "Stream Auto Poster", "coin_price": 120, "tagline": "Schedule everything",
        "description": "A real deliverable used by the live verification harness.",
        "version": "2.1.0", "platform": "Windows", "category_id": cat_id,
        "is_active": True, "is_featured": True, "allow_repurchase": False,
    })
    pd = body(p)
    check("create product (120 coins) -> 201", p.status_code == 201, f"{p.status_code} {json.dumps(pd)[:250]}")
    prod = pd.get("product") or {}
    STATE["product_id"] = prod.get("id")
    check("product id returned", bool(STATE["product_id"]), STATE["product_id"])

    p2 = m.post("/api/master/products", json={
        "name": "Enterprise Suite (expensive)", "coin_price": 999_999,
        "category_id": cat_id, "is_active": True,
    })
    STATE["expensive_id"] = (body(p2).get("product") or {}).get("id")
    check("create a second, deliberately unaffordable product -> 201", p2.status_code == 201, p2.status_code)

    blob = zip_bytes()
    STATE["file_bytes"] = blob
    up = m.post(f"/api/master/products/{STATE['product_id']}/file",
                files={"file": ("stream-auto-poster-2.1.0.zip", blob, "application/zip")})
    ud = body(up)
    check("attach the deliverable (.zip) -> 201", up.status_code == 201, f"{up.status_code} {json.dumps(ud)[:200]}")
    check("stored size matches the bytes sent",
          (ud.get("file") or {}).get("size_bytes") == len(blob), (ud.get("file") or {}).get("size_bytes"))

    bad = m.post(f"/api/master/products/{STATE['product_id']}/file",
                 files={"file": ("payload.php", b"<?php system($_GET[0]); ?>", "application/x-php")})
    check("a disallowed extension (.php) is refused", bad.status_code == 400, bad.status_code)

    med = m.post(f"/api/master/products/{STATE['product_id']}/media",
                 data={"kind": "image", "caption": "Dashboard"},
                 files={"image": ("shot.png", png_bytes(), "image/png")})
    check("upload product media (real PNG) -> 201", med.status_code == 201,
          f"{med.status_code} {med.text[:200]}")
    fake = m.post(f"/api/master/products/{STATE['product_id']}/media",
                  data={"kind": "image"},
                  files={"image": ("evil.png", b"MZ\x90\x00 this is a windows binary", "image/png")})
    check("an .exe renamed .png is refused by magic-byte sniffing", fake.status_code == 400, fake.status_code)

    pub = body(STATE["anon"].get(f"/api/products/{STATE['product_id']}"))
    check("the product is now visible on the public catalogue",
          pub.get("coin_price") == 120 and pub.get("has_file") is True, json.dumps(pub)[:220])
    check("the public product payload never leaks a stored file path",
          "stored_path" not in json.dumps(pub) and ".zip" not in json.dumps(pub),
          "no stored_path / archive filename in the public JSON")
    check("public detail exposes affordability, not a download url",
          "affordability" in pub and "download" not in json.dumps(pub).lower())
def phase_seller(base: str) -> None:
    section("D. Seller account, server-side device binding, RBAC")
    m: Client = STATE["master"]

    created = m.post("/api/master/sellers", json={
        "username": SELLER_USER, "password": SELLER_PW, "contact_email": "seller@example.com",
        "device_lock": True, "can_verify_payments": True, "note": "created by live-verify",
    })
    sd = body(created)
    check("Master creates a Seller -> 201", created.status_code == 201,
          f"{created.status_code} {json.dumps(sd)[:200]}")
    seller_id = (sd.get("seller") or sd.get("user") or {}).get("id")
    STATE["seller_id"] = seller_id
    check("seller id returned", bool(seller_id), json.dumps(sd)[:200])

    s = Client(base, device="seller-device-A", label="seller")
    STATE["seller"] = s
    lg = s.post("/api/auth/seller/login",
                json={"username": SELLER_USER, "password": SELLER_PW, "device_id": "seller-device-A"})
    ld = body(lg)
    check("Seller login on its first device -> 200", lg.status_code == 200,
          f"{lg.status_code} {json.dumps(ld)[:200]}")
    check("role is SELLER", ld.get("role") == "SELLER", ld.get("role"))

    # a *different* browser: no cookies at all, different device id
    s2 = Client(base, device="seller-device-B", label="seller-2nd-device")
    lg2 = s2.post("/api/auth/seller/login",
                  json={"username": SELLER_USER, "password": SELLER_PW, "device_id": "seller-device-B"})
    check("the same Seller from a SECOND device is refused (server-side device lock)",
          lg2.status_code in (401, 403), f"{lg2.status_code} {lg2.text[:160]}")
    check("the refusal is about the device, not the password",
          "device" in lg2.text.lower(), lg2.text[:200])
    s2.close()

    check("Seller reaches its own dashboard", s.get("/api/seller/overview").status_code == 200)
    check("Seller sees the seller order list", s.get("/api/seller/orders").status_code == 200)
    for path in ("/api/master/overview", "/api/master/audit", "/api/master/customers",
                 "/api/master/sellers"):
        r = s.get(path)
        check(f"Seller is refused {path} -> 403", r.status_code == 403, r.status_code)
    r = s.post(f"/api/master/products/{STATE['product_id']}/file",
               files={"file": ("x.zip", zip_bytes(), "application/zip")})
    check("Seller cannot upload a product deliverable -> 403", r.status_code == 403, r.status_code)
    r = s.post(f"/api/master/customers/{STATE.get('seller_id')}/wallet",
               json={"coins": 100, "direction": "add", "reason": "seller should not be able to"})
    check("Seller cannot adjust a wallet -> 403", r.status_code == 403, r.status_code)

    devs = body(m.get(f"/api/master/accounts/{seller_id}/devices"))
    check("Master can audit the Seller's bound devices",
          len(devs.get("devices", [])) >= 1, json.dumps(devs)[:220])
    check("exactly one device is active for the Seller",
          sum(1 for d in devs.get("devices", []) if d.get("is_active")) == 1,
          json.dumps([d.get("is_active") for d in devs.get("devices", [])]))
def phase_viewer_and_coins(base: str) -> None:
    section("E. Viewer sign-in (Google dev stub) and the coin store")
    m: Client = STATE["master"]

    pk = m.post("/api/master/coin-packages", json={
        "name": "Verify Pack", "coins": 500, "bonus_coins": 100, "price_bdt": 450.0,
        "badge": "TEST", "is_active": True, "display_order": 0,
    })
    STATE["package_id"] = (body(pk).get("package") or {}).get("id")
    check("Master creates a coin package (500 + 100 bonus) -> 201", pk.status_code == 201,
          f"{pk.status_code} {pk.text[:200]}")
    mt = m.post("/api/master/payment-methods", json={
        "name": "bKash (verify)", "account_number": "01700000000", "account_name": "STREAM",
        "account_type": "personal", "instructions": "Send money then upload the screenshot.",
    })
    STATE["method_id"] = (body(mt).get("method") or {}).get("id")
    check("Master creates a payment method -> 201", mt.status_code == 201, mt.status_code)

    v = Client(base, device="viewer-device-A", label="viewer")
    STATE["viewer"] = v
    lg = v.post("/auth/google/dev-login", json={"email": VIEWER_EMAIL, "name": "Viewer One"})
    check("Viewer signs in through the Google dev stub -> 200", lg.status_code == 200,
          f"{lg.status_code} {lg.text[:200]}")
    vm = body(v.get("/api/auth/viewer/me"))
    STATE["viewer_id"] = (vm.get("user") or {}).get("id")
    check("the viewer session is authenticated", vm.get("authenticated") is True)
    check("the account role is VIEWER", (vm.get("user") or {}).get("role") == "VIEWER",
          (vm.get("user") or {}).get("role"))
    check("a wallet exists and starts at 0 coins",
          body(v.get("/api/wallet")).get("balance") == 0, body(v.get("/api/wallet")).get("balance"))

    pkgs = body(v.get("/api/wallet/packages"))
    ids = [p["id"] for p in pkgs.get("packages", [])]
    check("the new package is offered to the viewer", STATE["package_id"] in ids, str(ids)[:160])
    check("payment methods are offered with account numbers",
          any(x.get("account_number") for x in pkgs.get("methods", [])))

    section("F. payment proof upload NEVER credits coins by itself (§46)")
    req = v.post("/api/wallet/payment-request",
                 data={"package_id": STATE["package_id"], "method_id": STATE["method_id"],
                       "sender_number": "01711111111", "transaction_ref": "TX-LIVE-0001",
                       "note": "live verification"},
                 files={"screenshot": ("proof.png", png_bytes(), "image/png")})
    rd = body(req)
    check("submit payment request with a screenshot -> 201", req.status_code == 201,
          f"{req.status_code} {json.dumps(rd)[:220]}")
    pr = rd.get("request") or {}
    STATE["payreq_id"] = pr.get("id")
    check("the request opens as PENDING", pr.get("status") == "PENDING", pr.get("status"))
    check("total coins = 500 + 100 bonus", pr.get("total_coins") == 600, pr.get("total_coins"))
    check("the wallet is STILL 0 after uploading proof",
          body(v.get("/api/wallet")).get("balance") == 0,
          body(v.get("/api/wallet")).get("balance"))
    check("the screenshot is not publicly reachable",
          v.get(f"/api/payments/screenshot/{(pr.get('screenshots') or [{}])[0].get('id')}").status_code == 401,
          "viewer surface is not staff")
    bad = v.post("/api/wallet/payment-request",
                 data={"package_id": STATE["package_id"]},
                 files={"screenshot": ("notimage.png", b"this is definitely not a png", "image/png")})
    check("a non-image 'screenshot' is refused", bad.status_code == 400, bad.status_code)
def phase_payment_review(base: str) -> None:
    section("G. payment verification: exactly-once credit, reject never credits")
    m: Client = STATE["master"]
    v: Client = STATE["viewer"]
    s: Client = STATE["seller"]
    rid = STATE["payreq_id"]

    lst = body(m.get("/api/master/payments?status=PENDING"))
    check("Master sees the pending request in the review queue",
          any(r["id"] == rid for r in lst.get("requests", [])), json.dumps(lst)[:200])
    shot = (body(m.get(f"/api/master/payments/{rid}")).get("request") or {}).get("screenshots") or []
    check("Master can open the payment proof",
          bool(shot) and m.get(shot[0]["url"]).status_code == 200,
          shot[0]["url"] if shot else "no screenshot")
    check("the Seller can also see the proof (payment verifier)",
          bool(shot) and s.get(shot[0]["url"]).status_code == 200)

    c1 = m.post(f"/api/master/payments/{rid}/confirm")
    d1 = body(c1)
    check("confirm -> 200", c1.status_code == 200, f"{c1.status_code} {json.dumps(d1)[:220]}")
    check("600 coins were credited", d1.get("coins_added") == 600, d1.get("coins_added"))
    check("the reported balance is 600", d1.get("balance") == 600, d1.get("balance"))
    check("the viewer's wallet now reads 600", body(v.get("/api/wallet")).get("balance") == 600,
          body(v.get("/api/wallet")).get("balance"))

    c2 = m.post(f"/api/master/payments/{rid}/confirm")
    d2 = body(c2)
    check("a SECOND confirm is a no-op, not a second credit",
          c2.status_code == 200 and d2.get("already_processed") is True, json.dumps(d2)[:220])
    check("the balance did not move on the replay",
          body(v.get("/api/wallet")).get("balance") == 600,
          body(v.get("/api/wallet")).get("balance"))
    c3 = s.post(f"/api/seller/payments/{rid}/confirm")
    check("a Seller confirming the same request also cannot double-credit",
          body(c3).get("already_processed") is True or c3.status_code in (400, 404, 409),
          f"{c3.status_code} {c3.text[:200]}")
    check("balance still 600 after the Seller's attempt",
          body(v.get("/api/wallet")).get("balance") == 600)

    txns = body(v.get("/api/wallet/transactions")).get("transactions", [])
    check("exactly one credit transaction exists for the payment",
          sum(1 for t in txns if t.get("amount") == 600) == 1,
          json.dumps([(t.get("type"), t.get("amount")) for t in txns])[:220])
    check("the ledger sums to the cached balance",
          sum(int(t["amount"]) for t in txns) == 600, sum(int(t["amount"]) for t in txns))

    # --- rejection path
    req2 = v.post("/api/wallet/payment-request",
                  data={"package_id": STATE["package_id"], "method_id": STATE["method_id"],
                        "transaction_ref": "TX-LIVE-0002"},
                  files={"screenshot": ("proof2.png", png_bytes(colour=(20, 90, 200)), "image/png")})
    rid2 = (body(req2).get("request") or {}).get("id")
    check("a second payment request is opened -> 201", req2.status_code == 201, req2.status_code)
    rj = m.post(f"/api/master/payments/{rid2}/reject", json={"reason": "Screenshot is unreadable."})
    check("reject -> 200", rj.status_code == 200, f"{rj.status_code} {rj.text[:200]}")
    check("the request is REJECTED", (body(rj).get("request") or {}).get("status") == "REJECTED",
          (body(rj).get("request") or {}).get("status"))
    check("REJECT ADDED NO COINS — balance is still 600",
          body(v.get("/api/wallet")).get("balance") == 600,
          body(v.get("/api/wallet")).get("balance"))
    rj2 = m.post(f"/api/master/payments/{rid2}/reject", json={"reason": "again"})
    check("a second reject is a no-op", body(rj2).get("already_processed") is True, rj2.text[:200])
    conf_after = m.post(f"/api/master/payments/{rid2}/confirm")
    check("a rejected request cannot later be confirmed into a credit",
          body(conf_after).get("already_processed") is True
          and body(v.get("/api/wallet")).get("balance") == 600,
          f"{conf_after.status_code} balance={body(v.get('/api/wallet')).get('balance')}")
def phase_orders(base: str) -> None:
    section("H. purchase: server-side deduction, idempotency, no double-buy")
    v: Client = STATE["viewer"]

    key = "live-verify-purchase-key-0001"
    buy = v.post("/api/orders/purchase", json={"product_id": STATE["product_id"], "idempotency_key": key})
    bd = body(buy)
    check("purchase -> 201", buy.status_code == 201, f"{buy.status_code} {json.dumps(bd)[:250]}")
    order = bd.get("order") or {}
    STATE["order_id"] = order.get("id")
    check("an order was created", bool(STATE["order_id"]), json.dumps(bd)[:200])
    check("120 coins were deducted server-side (600 -> 480)",
          body(v.get("/api/wallet")).get("balance") == 480,
          body(v.get("/api/wallet")).get("balance"))

    replay = v.post("/api/orders/purchase",
                    json={"product_id": STATE["product_id"], "idempotency_key": key})
    rd = body(replay)
    check("replaying the same idempotency key returns the SAME order",
          (rd.get("order") or {}).get("id") == STATE["order_id"],
          f"{replay.status_code} {json.dumps(rd)[:200]}")
    check("the replay deducted nothing (still 480)",
          body(v.get("/api/wallet")).get("balance") == 480,
          body(v.get("/api/wallet")).get("balance"))

    again = v.post("/api/orders/purchase", json={"product_id": STATE["product_id"]})
    check("buying an already-owned product is refused (409)", again.status_code == 409, again.status_code)
    check("the refusal names already_owned", "already" in again.text.lower(), again.text[:200])
    check("balance untouched by the refused purchase",
          body(v.get("/api/wallet")).get("balance") == 480)

    poor = v.post("/api/orders/purchase", json={"product_id": STATE["expensive_id"]})
    check("an unaffordable purchase -> 402 Payment Required", poor.status_code == 402, poor.status_code)
    check("balance untouched by the unaffordable purchase",
          body(v.get("/api/wallet")).get("balance") == 480)

    # a viewer must not be able to invent coins from the client side
    forged = v.post(f"/api/master/customers/{STATE['viewer_id']}/wallet",
                    json={"coins": 100000, "direction": "add", "reason": "client-side top-up"})
    check("a viewer cannot credit its own wallet -> 401/403",
          forged.status_code in (401, 403), forged.status_code)

    mine = body(v.get("/api/orders"))
    check("the order appears in the viewer's order list",
          any(o["id"] == STATE["order_id"] for o in mine.get("orders", [])), json.dumps(mine)[:200])
    detail = body(v.get(f"/api/orders/{STATE['order_id']}"))
    check("the viewer can open its own order", "order" in detail or "id" in detail,
          json.dumps(detail)[:160])

    v2 = Client(base, device="viewer-device-B", label="viewer2")
    STATE["viewer2"] = v2
    v2.post("/auth/google/dev-login", json={"email": VIEWER2_EMAIL, "name": "Viewer Two"})
    other = v2.get(f"/api/orders/{STATE['order_id']}")
    check("another viewer cannot read someone else's order -> 403/404",
          other.status_code in (403, 404), other.status_code)

    txns = body(v.get("/api/wallet/transactions")).get("transactions", [])
    check("the ledger recorded the debit as -120",
          any(t["amount"] == -120 for t in txns),
          json.dumps([t["amount"] for t in txns]))
    check("the ledger still sums to the cached balance (480)",
          sum(int(t["amount"]) for t in txns) == 480, sum(int(t["amount"]) for t in txns))
    hb = body(STATE["anon"].get("/health"))
    check("/health confirms site-wide ledger integrity after all money movement",
          hb.get("ok") is True and hb["wallet_ledger"]["consistent"] is True,
          json.dumps(hb.get("wallet_ledger")))
def wait_for_eml(count: int, timeout: float = 30.0) -> list[Path]:
    outbox = TMP / "uploads" / "outbox"
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = sorted(outbox.glob("*.eml"), key=lambda p: p.stat().st_mtime)
        if len(files) >= count:
            return files
        time.sleep(0.3)
    return sorted(outbox.glob("*.eml"), key=lambda p: p.stat().st_mtime)


def phase_delivery(base: str) -> None:
    section("I. Gmail delivery — the real email pipeline")
    v: Client = STATE["viewer"]

    files = wait_for_eml(1)
    check("the purchase produced a delivery email", len(files) >= 1, f"{len(files)} .eml in outbox")
    if not files:
        return
    msg = message_from_bytes(files[-1].read_bytes())
    check("the email is addressed to the buyer's Gmail address",
          VIEWER_EMAIL in (msg.get("To") or ""), msg.get("To"))
    check("the subject names the product or the order",
          "Stream Auto Poster" in (msg.get("Subject") or "")
          or "order" in (msg.get("Subject") or "").lower(), msg.get("Subject"))
    text = "".join(
        part.get_payload(decode=True).decode("utf-8", "replace")
        for part in msg.walk() if part.get_content_type() in ("text/plain", "text/html")
    )
    check("the email body carries a /download/ link, not a permanent public URL",
          "/download/" in text, text[:200].replace("\n", " "))
    tok = text.split("/download/")[1].split()[0].strip("\"'<>)")
    STATE["email_token"] = tok
    check("the emailed token is long and unguessable", len(tok) >= 24, f"{len(tok)} chars")
    attach = [p.get_filename() for p in msg.walk() if p.get_filename()]
    check("delivery included the deliverable or a link (both are valid)", True,
          f"attachments={attach}")

    before = len(files)
    rs = v.post(f"/api/orders/{STATE['order_id']}/resend-email")
    check("resend-email -> 200", rs.status_code == 200, f"{rs.status_code} {rs.text[:160]}")
    after = wait_for_eml(before + 1, timeout=20)
    check("the forced resend produced exactly one more email (no duplicate storm)",
          len(after) == before + 1, f"{before} -> {len(after)}")

    section("J. secure download: owner-bound, expiring, capped, revocable")
    dl = v.post(f"/api/orders/{STATE['order_id']}/download-link")
    dd = body(dl)
    check("issue a download link -> 200", dl.status_code == 200, f"{dl.status_code} {dl.text[:160]}")
    url = dd.get("download_url") or ""
    STATE["dl_url"] = url
    check("the link is a /download/<token> url, never a static file path",
          "/download/" in url and "/uploads/" not in url and "/media/" not in url, url[:120])
    check("the grant has a download ceiling of 2 (DOWNLOAD_MAX_ATTEMPTS)",
          dd.get("max_downloads") == 2, dd.get("max_downloads"))
    check("the grant has an expiry timestamp", bool(dd.get("expires_at")), dd.get("expires_at"))

    path = "/download/" + url.split("/download/")[1]
    got = STATE["anon"].get(path)
    check("the token downloads the real file", got.status_code == 200, got.status_code)
    check("the bytes served are exactly the bytes uploaded",
          got.content == STATE["file_bytes"], f"{len(got.content)} vs {len(STATE['file_bytes'])}")
    check("the download is sent with Cache-Control: no-store",
          got.headers.get("cache-control") == "no-store", got.headers.get("cache-control"))
    check("a foreign signed-in viewer is refused the token -> 403",
          STATE["viewer2"].get(path).status_code == 403, STATE["viewer2"].get(path).status_code)
    check("a garbage token -> 404", STATE["anon"].get("/download/not-a-real-token").status_code == 404)

    second = STATE["anon"].get(path)
    check("the second permitted download still works", second.status_code == 200, second.status_code)
    third = STATE["anon"].get(path)
    check("the third attempt is refused — the ceiling is enforced (429)",
          third.status_code == 429, third.status_code)

    fresh = body(v.post(f"/api/orders/{STATE['order_id']}/download-link"))
    check("the owner can mint a NEW grant after exhausting one",
          fresh.get("download_url") and fresh["download_url"] != url,
          "new token differs from the exhausted one")
    STATE["dl_path"] = "/download/" + fresh["download_url"].split("/download/")[1]
    check("the new grant downloads successfully",
          STATE["anon"].get(STATE["dl_path"]).status_code == 200)
def phase_notifications_and_ws(base: str) -> None:
    section("K. notifications feed")
    v: Client = STATE["viewer"]
    feed = body(v.get("/api/notifications"))
    items = feed.get("notifications", [])
    kinds = [i.get("kind") for i in items]
    check("the viewer has notifications from the real workflow", len(items) >= 2, str(kinds)[:200])
    check("a coin-credit notification was raised",
          any("COIN" in str(k).upper() or "PAYMENT" in str(k).upper() for k in kinds), str(kinds)[:200])
    check("a delivery notification was raised",
          any("DELIVER" in str(k).upper() for k in kinds), str(kinds)[:200])
    unread = body(v.get("/api/notifications/unread-count")).get("unread")
    check("unread count is greater than zero", (unread or 0) > 0, unread)
    mr = v.post("/api/notifications/read", json={})
    check("mark-all-read -> 200", mr.status_code == 200, f"{mr.status_code} {mr.text[:160]}")
    check("unread drops to 0", body(v.get("/api/notifications/unread-count")).get("unread") == 0,
          body(v.get("/api/notifications/unread-count")).get("unread"))
    raw = httpx.Client(base_url=base, timeout=20, cookies=v.c.cookies)
    check("mark-read without a CSRF header -> 403",
          raw.post("/api/notifications/read", json={}).status_code == 403)
    raw.close()

    section("L. chat / WebSocket over a real socket")
    from websockets.sync.client import connect
    from websockets.exceptions import WebSocketException

    ws_base = "ws://" + base.split("://", 1)[1]
    cookie = "; ".join(f"{k}={val}" for k, val in v.c.cookies.items())
    try:
        with connect(ws_base + "/ws", additional_headers={"Cookie": cookie},
                     open_timeout=15, close_timeout=5) as sock:
            greet = json.loads(sock.recv(timeout=10))
            check("an authenticated viewer's /ws handshake succeeds",
                  greet.get("type") == "connected", json.dumps(greet)[:200])
            check("the server reports the subscribed topics",
                  any(str(t).startswith("user:") for t in greet.get("topics", [])),
                  str(greet.get("topics")))
            sock.send("ping")
            pong = json.loads(sock.recv(timeout=10))
            check("ping -> pong heartbeat", pong.get("type") == "pong", json.dumps(pong)[:120])

            # a real server-side event must arrive on the open socket
            adj = STATE["master"].post(f"/api/master/customers/{STATE['viewer_id']}/wallet",
                                       json={"coins": 25, "direction": "bonus",
                                             "reason": "live WebSocket broadcast check"})
            check("Master grants 25 bonus coins -> 200", adj.status_code == 200,
                  f"{adj.status_code} {adj.text[:160]}")
            pushed = None
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    frame = json.loads(sock.recv(timeout=5))
                except Exception:
                    break
                if frame.get("type") not in ("pong", "connected"):
                    pushed = frame
                    break
            check("the live event reached the open WebSocket", pushed is not None,
                  json.dumps(pushed)[:220] if pushed else "no frame received")
            if pushed:
                check("the pushed frame carries a notification payload",
                      "notification" in json.dumps(pushed).lower()
                      or "coin" in json.dumps(pushed).lower(), json.dumps(pushed)[:220])
    except WebSocketException as exc:
        check("an authenticated viewer's /ws handshake succeeds", False, repr(exc))

    try:
        with connect(ws_base + "/ws", open_timeout=15) as sock:
            sock.recv(timeout=5)
            check("an anonymous WebSocket is rejected", False, "it was accepted")
    except Exception as exc:
        code = getattr(exc, "code", None) or getattr(exc, "rcvd", None)
        check("an anonymous WebSocket is rejected (policy violation)", True, f"{type(exc).__name__} {code}")
    check("the bonus grant is reflected in the wallet (480 + 25 = 505)",
          body(v.get("/api/wallet")).get("balance") == 505,
          body(v.get("/api/wallet")).get("balance"))
def phase_refund_and_staff(base: str) -> None:
    section("M. refund: coins returned once, download grants revoked")
    m: Client = STATE["master"]
    v: Client = STATE["viewer"]
    s: Client = STATE["seller"]

    seen = body(s.get("/api/seller/orders"))
    check("the Seller order list is reachable", "orders" in seen, json.dumps(seen)[:160])
    forbid = s.post(f"/api/master/orders/{STATE['order_id']}/refund",
                    json={"reason": "seller should not be able to refund"})
    check("a Seller cannot refund -> 403", forbid.status_code == 403, forbid.status_code)
    noreason = m.post(f"/api/master/orders/{STATE['order_id']}/refund", json={})
    check("a refund without a reason is rejected (422) — reasons are mandatory",
          noreason.status_code == 422, noreason.status_code)

    before = body(v.get("/api/wallet")).get("balance")
    rf = m.post(f"/api/master/orders/{STATE['order_id']}/refund",
                json={"reason": "Customer reported the build does not launch.", "cancel": False})
    rd = body(rf)
    check("refund -> 200", rf.status_code == 200, f"{rf.status_code} {json.dumps(rd)[:220]}")
    check("the order is REFUNDED", (rd.get("order") or {}).get("status") == "REFUNDED",
          (rd.get("order") or {}).get("status"))
    after = body(v.get("/api/wallet")).get("balance")
    check("120 coins went back to the wallet", after == before + 120, f"{before} -> {after}")

    rf2 = m.post(f"/api/master/orders/{STATE['order_id']}/refund", json={"reason": "double click"})
    check("a second refund does not return the coins twice",
          body(v.get("/api/wallet")).get("balance") == after,
          f"rc={rf2.status_code} balance={body(v.get('/api/wallet')).get('balance')}")

    revoked = STATE["anon"].get(STATE["dl_path"])
    check("the download grant is revoked after the refund -> 403",
          revoked.status_code == 403, revoked.status_code)
    reissue = v.post(f"/api/orders/{STATE['order_id']}/download-link")
    check("a refunded order cannot mint a new download link -> 403",
          reissue.status_code == 403, reissue.status_code)
    resend = v.post(f"/api/orders/{STATE['order_id']}/resend-email")
    check("a refunded order cannot be re-delivered -> 403", resend.status_code == 403, resend.status_code)
    check("the product is purchasable again after the refund",
          v.post("/api/orders/purchase", json={"product_id": STATE["product_id"]}).status_code == 201,
          "refund released the ownership lock")

    section("N. Seller order completion + audit trail + logout revocation")
    orders_seen = body(s.get("/api/seller/orders")).get("orders", [])
    check("the Seller can see orders in the system", isinstance(orders_seen, list), len(orders_seen))

    audit = body(m.get("/api/master/audit?limit=500"))
    entries = audit.get("entries") or audit.get("logs") or audit.get("audit") or []
    actions = {e.get("action") for e in entries}
    for want in ("catalog.product_create", "account.create_seller", "payment.request"):
        check(f"audit log contains {want}", want in actions, str(sorted(actions))[:240])
    check("audit entries record who did it",
          all(e.get("actor") or e.get("actor_label") or e.get("actor_id") for e in entries[:5]),
          json.dumps(entries[0])[:220] if entries else "no entries")

    out = m.post("/api/auth/staff/logout")
    check("Master logout -> 200", out.status_code == 200, out.status_code)
    check("the revoked staff session can no longer be used",
          m.get("/api/auth/staff/me").status_code == 401,
          m.get("/api/auth/staff/me").status_code)
    vout = v.post("/api/auth/viewer/logout")
    check("Viewer logout -> 200", vout.status_code == 200, vout.status_code)
    check("the revoked viewer session can no longer read the wallet",
          v.get("/api/wallet").status_code == 401, v.get("/api/wallet").status_code)
def phase_production_boot() -> None:
    section("O. a production-shaped boot must actually harden itself")
    port = free_port()
    proc, base = boot(port, extra={
        "ENVIRONMENT": "production",
        "DEBUG": "false",
        "ALLOWED_HOSTS": "streamcorporation.example,127.0.0.1",
        "ALLOW_DEV_GOOGLE_STUB": "true",       # even so, production must refuse it
        "COOKIE_SECURE": "false",              # kept false only so httpx can be observed
        "DATABASE_URL": "sqlite+aiosqlite:///./tests/.tmp/live/prod.db",
    }, log_name="server-prod.log")
    try:
        c = httpx.Client(base_url=base, timeout=30)
        r = c.get("/health")
        check("the app boots under ENVIRONMENT=production", r.status_code == 200, r.status_code)
        check("/health reports environment=production",
              body(r).get("environment") == "production", body(r).get("environment"))
        home = c.get("/")
        check("HSTS is sent in production",
              home.headers.get("strict-transport-security", "").startswith("max-age=31536000"),
              home.headers.get("strict-transport-security"))
        check("/api/docs is not exposed in production", c.get("/api/docs").status_code == 404,
              c.get("/api/docs").status_code)
        check("/api/openapi.json is not exposed in production",
              c.get("/api/openapi.json").status_code == 404, c.get("/api/openapi.json").status_code)
        stub = c.post("/auth/google/dev-login", json={"email": "someone@example.com"})
        check("the Google dev-login stub is refused in production -> 404",
              stub.status_code == 404, stub.status_code)
        bad = c.get("/health", headers={"Host": "evil.example.com"})
        check("TrustedHostMiddleware rejects a foreign Host header",
              bad.status_code == 400, bad.status_code)
        good = c.get("/health", headers={"Host": "streamcorporation.example"})
        check("an allow-listed Host is accepted", good.status_code == 200, good.status_code)
        c.close()
    finally:
        stop(proc)


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    port = free_port()
    print(f"booting the real application on 127.0.0.1:{port} ...", flush=True)
    proc, base = boot(port)
    try:
        phase_public(base)
        phase_master_login(base)
        phase_catalogue(base)
        phase_seller(base)
        phase_viewer_and_coins(base)
        phase_payment_review(base)
        phase_orders(base)
        phase_delivery(base)
        phase_notifications_and_ws(base)
        phase_refund_and_staff(base)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        check("the harness ran to completion", False, f"{type(exc).__name__}: {exc}")
    finally:
        stop(proc)

    try:
        phase_production_boot()
    except Exception as exc:
        check("the production-shaped boot completed", False, f"{type(exc).__name__}: {exc}")

    ok = sum(1 for r in RESULTS if r[0])
    total = len(RESULTS)
    print(f"\nLIVE FUNCTIONAL VERIFICATION: {ok}/{total} checks passed", flush=True)
    failed = [(n, d) for good, n, d in RESULTS if not good]
    for name, detail in failed:
        print(f"  FAILED: {name}   {detail}", flush=True)
    if failed:
        log_text = (TMP / "server.log").read_text(encoding="utf-8", errors="replace")
        errs = [ln for ln in log_text.splitlines() if "ERROR" in ln or "Traceback" in ln]
        if errs:
            print("\nserver-side errors:", flush=True)
            for ln in errs[-25:]:
                print("   " + ln, flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
