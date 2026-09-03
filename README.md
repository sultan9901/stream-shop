<div align="center">

# STREAM CORPORATION

**Premium Coin-Based Software Marketplace**

Python · FastAPI · PostgreSQL · SQLAlchemy · Alembic · Redis · WebSocket

Master + Seller + Google Viewer authentication · manual screenshot payment verification ·
append-only coin ledger · secure expiring downloads · server-side device binding

</div>

---

## What this is

A real, working full-stack marketplace — not a mockup. Viewers sign in with Google, buy
**Coins** with BDT (bKash / Nagad / … verified by hand from an uploaded screenshot), then
spend those Coins on software. Every purchase deducts Coins **server side inside one
database transaction**, creates an order, notifies Master and Seller in real time over
WebSocket, emails the customer, and issues a **tokenised, owner-bound, expiring** download
link. Refunds return the Coins to the ledger and kill the download grants.

The rules that matter are enforced in the backend and covered by tests:

| Guarantee | Where it lives | Proven by |
|---|---|---|
| Passwords are Argon2id, never plaintext | `backend/app/auth/security.py` | §15 checklist |
| Device lock is server-side, not IP-based | `backend/app/auth/devices.py` | `tests/test_device_lock.py` |
| Uploading a screenshot never credits Coins | `backend/app/payments/service.py` | `tests/test_payments_idempotency.py` |
| Confirming twice never double-credits | conditional `UPDATE … WHERE status='PENDING'` | `tests/test_payments_idempotency.py` |
| Coin deduction is server-side and locked | `backend/app/wallet/service.py` | `tests/test_purchase.py` |
| Concurrent double-click buys once | unique `idempotency_key` + row lock | `tests/test_purchase.py` |
| Balance is never an editable number | append-only `wallet_transactions` | `tests/test_wallet_ledger.py` |
| Download links are not public permanent URLs | `backend/app/delivery/tokens.py` | `tests/test_download.py` |
| Every manual adjustment needs a reason | `WalletAdjustIn.reason` (min 3 chars) | §15 checklist |

---

# 1. Complete source code

Everything is in this repository. Nothing is fetched from a private registry, and there is
no compiled or obfuscated blob anywhere — the whole system is readable Python, Jinja2, CSS
and vanilla JavaScript.

| Layer | Location | Notes |
|---|---|---|
| Backend | `backend/app/` | FastAPI application, one package per domain |
| Frontend | `frontend/templates/`, `frontend/static/` | Jinja2 server-rendered pages + vanilla CSS/JS, no build step |
| Migrations | `migrations/versions/` | Alembic, async engine |
| Tests | `tests/` | pytest, drives the real ASGI app |
| Config template | `.env.example` | copy to `.env` |
| Containers | `Dockerfile`, `docker-compose.yml`, `docker/entrypoint.sh` | |

Entry point: `backend/app/main.py` → `app = create_app()`.

---

# 2. Full folder structure

```text
STREAM-CORPORATION/
│
├── backend/
│   ├── app/
│   │   ├── main.py                  # create_app(), middleware, error pages, lifespan
│   │   ├── config.py                # pydantic-settings Settings (reads .env)
│   │   ├── database.py              # async engine, session factory, init_db()
│   │   ├── templating.py            # Jinja2 environment
│   │   │
│   │   ├── models/                  # SQLAlchemy 2.0 ORM
│   │   │   ├── base.py              # Base, UUIDPk, TimestampMixin, TZDateTime, enums
│   │   │   ├── user.py              # users, master_accounts, seller_accounts, viewer_profiles
│   │   │   ├── device.py            # devices, sessions, login_attempts
│   │   │   ├── product.py           # categories, products, product_media, product_files
│   │   │   ├── wallet.py            # wallets, wallet_transactions, coin_packages
│   │   │   ├── payment.py           # payment_methods, payment_requests, payment_screenshots
│   │   │   ├── order.py             # orders, order_items, deliveries, download_tokens, download_logs
│   │   │   ├── notification.py      # notifications
│   │   │   └── system.py            # settings, audit_logs, counters
│   │   │
│   │   ├── schemas/                 # Pydantic v2 request/response models
│   │   ├── routes/                  # HTTP surface (one module per area)
│   │   ├── auth/                    # sessions, Argon2 hashing, devices, Google OAuth, codes
│   │   ├── wallet/                  # the coin ledger
│   │   ├── payments/                # BDT payment requests + verification state machine
│   │   ├── orders/                  # purchase transaction, lifecycle, refunds
│   │   ├── delivery/                # email delivery + secure download tokens
│   │   ├── notifications/           # in-app notification fan-out
│   │   ├── websocket/               # connection manager + Redis pub/sub bridge
│   │   └── services/                # uploads, audit log, cache/rate limit, seed, settings
│   │
│   ├── requirements.txt             # runtime pins
│   └── requirements-dev.txt         # runtime + pytest
│
├── frontend/
│   ├── templates/                   # base, storefront, product, wallet, orders, master, seller
│   └── static/
│       ├── css/                     # core.css + page styles (cyber grid, glass, neon)
│       ├── js/                      # api client, wallet, purchase, dashboards, websocket
│       ├── images/
│       └── icons/
│
├── uploads/                         # runtime state — NOT in git
│   ├── screenshots/                 # payment proofs (staff-only endpoint)
│   ├── products/                    # deliverables (never served statically)
│   ├── media/                       # public product images  -> /media
│   └── outbox/                      # .eml files when EMAIL_BACKEND=console
│
├── migrations/
│   ├── env.py                       # async Alembic env, DSN from app.config
│   └── versions/
│
├── tests/                           # pytest suite (44 tests)
├── docker/
│   └── entrypoint.sh                # validate config -> wait for DB -> migrate -> uvicorn
│
├── .env.example
├── .dockerignore
├── alembic.ini
├── pytest.ini
├── Dockerfile
├── docker-compose.yml
└── README.md
```

Only `uploads/media` is mounted as a static route (`/media`). `uploads/products` and
`uploads/screenshots` are **never** statically served — they are reachable only through the
tokenised `/download/{token}` route and the staff-only screenshot endpoint.

---

# 3. Database schema

26 tables. The 21 required by the specification are all present; the remaining five
(`categories`, `counters`, `download_tokens`, `download_logs`, `payment_methods`,
`login_attempts`) carry the download-security, human-readable-code and brute-force-defence
features.

### Identity and access

| Table | Purpose | Key columns |
|---|---|---|
| `users` | one row per human, any role | `id`, `role`, `public_code`, `username`, `email`, `password_hash`, `is_active`, `locked_until`, `failed_logins`, `must_change_password` |
| `master_accounts` | Master-only profile | `user_id`, `can_manage_masters`, `can_manage_sellers` |
| `seller_accounts` | Seller-only profile | `user_id`, `device_lock_enabled`, `can_verify_payments`, `contact_email` |
| `viewer_profiles` | Google customer profile | `user_id`, `google_sub`, `avatar_url` |
| `devices` | bound devices (server-side lock) | `user_id`, `fingerprint`, `first_ip`, `last_ip`, `bound_at`, `last_seen_at`, `is_active` |
| `sessions` | opaque server-side sessions | `token_hash`, `user_id`, `surface`, `device_id`, `csrf_token`, `expires_at`, `revoked_at` |
| `login_attempts` | brute-force / audit trail | `identifier`, `ip`, `succeeded`, `reason`, `created_at` |

### Catalogue

| Table | Purpose |
|---|---|
| `categories` | product grouping, slug-addressed |
| `products` | name, slug, `coin_price`, `version`, `platform`, `stock`, `sold_count`, `allow_repurchase`, `is_active`, `seller_id` |
| `product_media` | images/banners (public, served from `/media`) |
| `product_files` | the deliverable: `stored_path`, `size_bytes`, `checksum_sha256` (never public) |

### Money

| Table | Purpose | Integrity rule |
|---|---|---|
| `coin_packages` | what a viewer can buy | `coins` + `bonus_coins`, `price_bdt` |
| `wallets` | cached balance only | `balance`, `version`, `lifetime_credited`, `lifetime_spent`, `is_frozen` |
| `wallet_transactions` | **the source of truth** | append-only; `idempotency_key` is **UNIQUE**; every row has `amount`, `balance_after`, `reason`, `performed_by_*` |
| `payment_methods` | Master's bKash/Nagad destinations | |
| `payment_requests` | "I sent ৳X" claim | `status` state machine `PENDING → CONFIRMED│REJECTED│CANCELLED`, `credited_txn_id` |
| `payment_screenshots` | uploaded proof | `stored_path`, `checksum_sha256` |

`wallets.balance` is a cache. It is only ever written together with a
`wallet_transactions` row, in the same transaction, behind a row lock. `SELECT
SUM(amount)` over the ledger must equal the cached balance, and that is not left to
trust: `/health` re-derives it site-wide on every probe with one grouped aggregate
(`wallet_service.audit_consistency_all()`), returning `ok: false` and naming the drifted
wallets if the two ever disagree. `audit_consistency(db, user_id)` does the same for a
single customer and backs the Master customer detail view.

### Orders and delivery

| Table | Purpose |
|---|---|
| `orders` | `order_code` (`SC-ORD-######`), `status`, `coin_total`, `debit_txn_id`, `refund_txn_id`, `paid_at`, `completed_at`, `refunded_at`, `refund_reason` |
| `order_items` | product snapshot at purchase time (name, version, price) |
| `deliveries` | email attempts per order: `status`, `email_to`, `sent_at`, `last_error` |
| `download_tokens` | `token_hash`, `order_id`, `user_id`, `expires_at`, `max_downloads`, `download_count`, `revoked` |
| `download_logs` | every attempt: `outcome`, `ip`, `user_agent` |

### System

| Table | Purpose |
|---|---|
| `notifications` | in-app feed; `audience` (`MASTER`/`SELLER`) or `user_id`, `kind`, `is_read` |
| `settings` | Master-editable key/value site settings |
| `audit_logs` | `action`, `actor`, `target_type`, `target_id`, `summary`, `ip`, `created_at` |
| `counters` | atomic sequence source for `SC-ORD-…`, `SC-PAY-…`, `SC-TXN-…` codes |

**Portability note.** Timestamps use a custom `TZDateTime` type (`models/base.py`) that
stores UTC and always returns timezone-aware values, so identical comparison code runs on
SQLite and PostgreSQL. `FOR UPDATE` row locks are applied on PostgreSQL only; SQLite
serialises writers itself.

---

# 4. Migration commands

Alembic reads the DSN from `app.config.settings` (i.e. from the environment / `.env`), so a
production password is never written into `alembic.ini`.

```bash
alembic upgrade head
```

| Task | Command |
|---|---|
| Apply everything | `alembic upgrade head` |
| Current revision | `alembic current` |
| History | `alembic history --verbose` |
| Detect model↔schema drift | `alembic check` |
| Autogenerate a revision | `alembic revision --autogenerate -m "add x"` |
| Roll back one step | `alembic downgrade -1` |
| Preview SQL without applying | `alembic upgrade head --sql` |

Run these from the repository root (that is where `alembic.ini` lives). Always read a
generated revision before applying it — `render_as_batch` is on so revisions apply to
SQLite too, but autogenerate still cannot infer data migrations.

Inside Docker the entrypoint runs `alembic upgrade head` before uvicorn starts, so a
`docker compose up` on a fresh volume creates the schema by itself. To migrate without
serving:

```bash
docker compose run --rm app migrate
```

**Alembic and the first boot.** For local convenience the app also builds the schema itself
when it starts against a completely empty database — you do not have to run Alembic before
your first `GET /`. When it does so it **stamps `alembic_version` at head**, so the database
never ends up in the "tables exist but look un-migrated" state where `alembic check` reports
drift and `alembic upgrade head` fails trying to re-create existing tables. If the database
already has application tables, startup leaves the schema completely alone — Alembic stays
authoritative. Practical consequence: after you add or change a model, run
`alembic revision --autogenerate` and `alembic upgrade head`; restarting uvicorn will not
silently reshape an existing database.

`alembic check` should report `No new upgrade operations detected.` on a healthy checkout —
that is the assertion that the migrations and the ORM models still describe the same schema.

---

# 5. `.env.example`

`.env.example` is tracked; **`.env` is in `.gitignore` and must never be committed.** Copy
and edit:

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"   # paste into SECRET_KEY
```

| Variable | Default | Meaning |
|---|---|---|
| `APP_NAME` | `STREAM CORPORATION` | brand shown in UI and emails |
| `ENVIRONMENT` | `development` | `production` enables HSTS, hides `/api/docs`, tightens checks |
| `DEBUG` | `true` | verbose errors; **false in production** |
| `BASE_URL` | `http://localhost:8000` | used to build OAuth redirects, download links, email links |
| `SECRET_KEY` | *(placeholder)* | signs cookies, hashes session/download tokens. ≥32 chars |
| `ALLOWED_HOSTS` | `*` | TrustedHost middleware; name real domains in production |
| `CORS_ORIGINS` | `http://localhost:8000` | comma-separated allowed origins |
| `DATABASE_URL` | `sqlite+aiosqlite:///./stream_corporation.db` | async DSN; `postgresql+asyncpg://…` in production |
| `REDIS_URL` | *(empty)* | rate limits + WebSocket fan-out; empty ⇒ in-process fallback |
| `SESSION_COOKIE_VIEWER` / `_STAFF` / `_DEVICE` | `sc_viewer` / `sc_staff` / `sc_device` | separate cookies per surface |
| `SESSION_TTL_HOURS` | `72` | viewer session lifetime |
| `STAFF_SESSION_TTL_HOURS` | `12` | Master/Seller session lifetime |
| `COOKIE_SECURE` | `false` | **must be `true` in production** |
| `COOKIE_SAMESITE` | `lax` | |
| `DEFAULT_MASTER_USERNAME` / `_PASSWORD` | `Admin` / `admin` | bootstrap account, created once |
| `GOOGLE_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | *(empty)* | see §8 |
| `ALLOW_DEV_GOOGLE_STUB` | `true` | dev-only login stub; **false in production** |
| `EMAIL_BACKEND` | `console` | `console` writes `.eml` to `uploads/outbox`; `smtp` really sends |
| `SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` / `_STARTTLS` / `_SSL` | Gmail defaults | see §9 |
| `EMAIL_FROM` / `EMAIL_FROM_NAME` | placeholder | envelope sender |
| `UPLOAD_DIR` | `./uploads` | storage root |
| `MAX_SCREENSHOT_MB` / `MAX_PRODUCT_FILE_MB` / `MAX_IMAGE_MB` | `8` / `200` / `10` | upload ceilings |
| `DOWNLOAD_TOKEN_TTL_HOURS` | `72` | download grant lifetime |
| `DOWNLOAD_MAX_ATTEMPTS` | `10` | downloads allowed per grant |
| `RATE_LIMIT_ENABLED` | `true` | |
| `LOGIN_RATE_LIMIT` / `UPLOAD_RATE_LIMIT` / `PURCHASE_RATE_LIMIT` | `10/5m` / `20/1h` / `30/1h` | |

Email credentials live **only** here — no password is hard-coded anywhere in the source.

---

# 6. Installation commands

Python **3.12 or newer** (developed and tested on 3.14.7). No Node.js, no bundler.

```bash
git clone <your-repo-url> "stream shop" && cd "stream shop"
```

Then create the virtualenv and install:

```bash
python -m venv .venv
```

| Platform | Activate | Install |
|---|---|---|
| Linux / macOS | `source .venv/bin/activate` | `pip install -r backend/requirements-dev.txt` |
| Windows (PowerShell) | `.venv\Scripts\Activate.ps1` | `pip install -r backend/requirements-dev.txt` |
| Windows (Git Bash) | `source .venv/Scripts/activate` | `pip install -r backend/requirements-dev.txt` |

`backend/requirements.txt` is the runtime set (fully pinned).
`backend/requirements-dev.txt` includes it and adds pytest — use it for local work.

Then configure and create the schema:

```bash
cp .env.example .env && alembic upgrade head
```

PostgreSQL and Redis are **optional for local development** — the defaults use SQLite and
an in-process rate limiter, so the app runs with nothing else installed.

---

# 7. Local run instructions

```bash
uvicorn app.main:app --app-dir backend --reload --port 8000
```

Open <http://localhost:8000>.

On first start the app creates the upload folders, builds the schema if the database is
empty (stamping `alembic_version` at head — see §4), and seeds the bootstrap Master, the
coin packages, the demo payment methods and the default site settings.

| Surface | URL |
|---|---|
| Storefront | `/` |
| Wallet (viewer) | `/wallet` |
| My orders (viewer) | `/orders` |
| Master dashboard | `/master` |
| Seller dashboard | `/seller` |
| Health probe | `/health` |
| OpenAPI docs | `/api/docs` (hidden when `ENVIRONMENT=production`) |

`/health` is safe to expose and is what an uptime monitor should watch:

```json
{
  "ok": true,
  "app": "STREAM CORPORATION",
  "environment": "development",
  "database": "up",
  "wallet_ledger": { "checked": true, "consistent": true, "drifted_wallets": 0, "sample": [] }
}
```

`ok` is `false` if the database is unreachable **or** any cached wallet balance has drifted
from the sum of its ledger rows; `sample` then names up to five offending wallets.

Signing in as a **viewer** without configuring Google: leave `GOOGLE_CLIENT_ID` empty and
`ALLOW_DEV_GOOGLE_STUB=true`, then the storefront's Google button posts to
`/auth/google/dev-login`, which creates/opens a local account for any email you type. This
stub is refused outright when `ENVIRONMENT=production`.

`WEB_CONCURRENCY`/`--workers` above 1 needs `REDIS_URL`, otherwise rate-limit counters and
WebSocket broadcasts are per-worker. A single reload worker is the normal dev setup.

---

# 8. Google OAuth setup

1. Open the [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials).
2. Create (or pick) a project → **Create credentials → OAuth client ID**.
3. Application type: **Web application**.
4. **Authorised redirect URIs** — add exactly the value you will put in
   `GOOGLE_REDIRECT_URI`:
   - development: `http://localhost:8000/auth/google/callback`
   - production: `https://your-domain.com/auth/google/callback`
5. On the **OAuth consent screen**, add the `email`, `profile` and `openid` scopes. While
   the app is in *Testing*, add your own Gmail address under **Test users**.
6. Copy the client ID and secret into `.env`:

```bash
GOOGLE_CLIENT_ID=1234567890-abcdefg.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-your-secret
GOOGLE_REDIRECT_URI=https://your-domain.com/auth/google/callback
ALLOW_DEV_GOOGLE_STUB=false
```

Flow implemented in `backend/app/auth/google.py` + `backend/app/routes/auth_google.py`:
`/auth/google/start` stores a signed, single-use `state` in a short-lived cookie and
redirects to Google; `/auth/google/callback` verifies `state`, exchanges the code for
tokens over HTTPS, reads `userinfo`, then finds-or-creates the viewer keyed on the stable
Google `sub` (not the email, so an address change cannot hijack an account) and opens a
server-side session.

The redirect URI must match Google's registration **character for character** — a trailing
slash or `http` vs `https` mismatch is the usual cause of `redirect_uri_mismatch`. Because
the URI is derived from `BASE_URL`, set `BASE_URL` to the public HTTPS origin in production.

---

# 9. SMTP / email setup

Two backends, chosen with `EMAIL_BACKEND`:

| Value | Behaviour | Use for |
|---|---|---|
| `console` | writes a complete `.eml` file into `uploads/outbox/` and logs the path | development, CI, demos — no credentials needed |
| `smtp` | really sends via `aiosmtplib` | production |

Gmail with an **App Password** (2-Step Verification must be on; your normal password will
not work):

```bash
EMAIL_BACKEND=smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your-16-char-app-password
SMTP_STARTTLS=true
SMTP_SSL=false
EMAIL_FROM=you@gmail.com
EMAIL_FROM_NAME=STREAM CORPORATION
```

Port 465 instead: set `SMTP_SSL=true` and `SMTP_STARTTLS=false`.

Delivery runs as a background task **after** the purchase response is returned, so a slow
mail server never blocks a purchase, and it is idempotent per order — a retry or a
`redeliver` never sends the same order's link twice by accident. Each attempt is recorded
in `deliveries` with its status and last error; Master can re-send from the order detail
view. The email contains the tokenised `/download/{token}` URL, never a filesystem path.

Credentials are read from the environment only. Nothing in `backend/` contains a password.

---

# 10. PostgreSQL setup

Production database. The app uses the **async** `asyncpg` driver, so the DSN scheme must be
`postgresql+asyncpg://`.

Create role, database and grants:

```bash
sudo -u postgres psql -c "CREATE ROLE stream LOGIN PASSWORD 'a-strong-password';" -c "CREATE DATABASE stream_corporation OWNER stream ENCODING 'UTF8';"
```

Then point the app at it and migrate:

```bash
DATABASE_URL=postgresql+asyncpg://stream:a-strong-password@127.0.0.1:5432/stream_corporation alembic upgrade head
```

Notes.

- URL-encode special characters in the password (`@` → `%40`, `#` → `%23`).
- With `docker compose`, the DSN is built for you and points at the `db` service.
- Do **not** use `psycopg2`/`postgresql://` — there is no sync engine in this project.
- On PostgreSQL the wallet row is locked with `SELECT … FOR UPDATE` before any balance is
  read for a mutation, which is what makes concurrent purchases serialise.
- Recommended production settings: `max_connections` ≥ `WEB_CONCURRENCY × pool_size + 10`,
  and `ALTER DATABASE stream_corporation SET timezone TO 'UTC';` (all timestamps are UTC).

---

# 11. Redis setup

Optional but recommended. Redis provides two things:

1. **Rate-limit counters** — shared across workers, so `LOGIN_RATE_LIMIT` is a real global
   limit rather than per-process.
2. **WebSocket fan-out** — a pub/sub channel so a notification produced by worker A reaches
   sockets held by worker B.

```bash
REDIS_URL=redis://localhost:6379/0
```

With a password or TLS: `redis://:password@host:6379/0`, `rediss://host:6380/0`.

Leaving `REDIS_URL` empty is fully supported: the app falls back to an in-process limiter
and in-process broadcast. That is correct for a single worker and wrong for several — with
`WEB_CONCURRENCY > 1` and no Redis, live notifications only reach part of your users. The
container entrypoint prints a warning for exactly that combination.

Redis is a cache and a bus. Nothing durable lives there — no balances, no orders, no
sessions (sessions are rows in `sessions`). Flushing Redis loses nothing but rate-limit
counters.

---

# 12. Docker setup

Two files plus an entrypoint: `Dockerfile` (multi-stage, non-root, healthchecked) and
`docker-compose.yml` (app + PostgreSQL 17 + Redis 8).

```bash
cp .env.example .env && docker compose up -d --build
```

Then open <http://localhost:8000> and watch the logs:

```bash
docker compose logs -f app
```

What happens on `up`: Postgres and Redis start and must report *healthy*; the app container
then validates its configuration, waits for Postgres with `pg_isready`, runs
`alembic upgrade head`, and finally execs uvicorn.

`.env` values you should set before the first `up`: `SECRET_KEY`, `BASE_URL`,
`ALLOWED_HOSTS`, `CORS_ORIGINS`, `DEFAULT_MASTER_PASSWORD`, and (for production)
`ENVIRONMENT=production`, `DEBUG=false`, `COOKIE_SECURE=true`,
`ALLOW_DEV_GOOGLE_STUB=false`. Compose **overrides** `DATABASE_URL`, `REDIS_URL` and
`UPLOAD_DIR` so the container always talks to the `db`/`redis` services.

Optional overrides read from your shell or `.env`:

| Variable | Default | Effect |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `stream` / `streampass` / `stream_corporation` | database bootstrap **and** the DSN handed to the app |
| `APP_BIND` | `127.0.0.1` | host interface the port is published on |
| `APP_PUBLISHED_PORT` | `8000` | host port |
| `WEB_CONCURRENCY` | `2` | uvicorn workers |

Useful commands:

| Task | Command |
|---|---|
| Build only | `docker compose build` |
| Migrate without serving | `docker compose run --rm app migrate` |
| Shell in the app container | `docker compose exec app bash` |
| Python REPL with the app importable | `docker compose run --rm app shell` |
| Tail logs | `docker compose logs -f app` |
| Stop, keep data | `docker compose down` |
| Stop and **destroy** data | `docker compose down -v` |

Image details: `python:3.14-slim-bookworm`; a builder stage compiles wheels into
`/opt/venv` and the runtime stage copies only that venv, so no compiler or `-dev` headers
ship; the process runs as uid 10001 (`stream`); `/app/uploads` is a named volume because
screenshots, product files and console-backend mail are the only state outside Postgres;
`HEALTHCHECK` polls `/health`; and `.dockerignore` keeps `.env`, `.venv/`, `uploads/`,
`*.db` and `.git/` out of the build context entirely.

Behind a reverse proxy the app is already started with `--proxy-headers`, so
`X-Forwarded-For` / `X-Forwarded-Proto` are honoured (correct client IPs in the audit log,
correct scheme in generated links). Narrow `FORWARDED_ALLOW_IPS` from `*` to your proxy's
address when the container is reachable from anywhere but that proxy.

> Not verified on this machine: Docker is not installed here, so the image has not been
> built. The files are written against the versions this project actually runs on, but the
> first `docker compose up --build` should be watched.

---

# 13. Production deployment guide

```text
Internet → HTTPS (Let's Encrypt) → Nginx/Caddy reverse proxy → uvicorn (FastAPI)
                                                                  ├── PostgreSQL 17
                                                                  ├── Redis 8
                                                                  └── uploads volume
```

**1. Provision.** A small VM (2 vCPU / 2 GB) is enough to start. Install Docker Engine and
the Compose plugin. Point your domain's `A`/`AAAA` records at it.

**2. Configure.** Clone the repo, `cp .env.example .env`, then set at minimum:

```bash
ENVIRONMENT=production
DEBUG=false
BASE_URL=https://your-domain.com
SECRET_KEY=<python -c "import secrets;print(secrets.token_urlsafe(64))">
ALLOWED_HOSTS=your-domain.com,www.your-domain.com
CORS_ORIGINS=https://your-domain.com
COOKIE_SECURE=true
ALLOW_DEV_GOOGLE_STUB=false
DEFAULT_MASTER_PASSWORD=<a real password>
POSTGRES_PASSWORD=<a real password>
EMAIL_BACKEND=smtp
```

The entrypoint refuses to boot in production if `SECRET_KEY` is short, `COOKIE_SECURE` is
not `true`, or `ALLOWED_HOSTS` is still `*`.

**3. Start.** `docker compose up -d --build`, then `docker compose logs -f app` and confirm
`{"ok":true,…}` from `curl -s localhost:8000/health`.

**4. Terminate TLS in front.** Compose publishes to `127.0.0.1:8000` only. Minimal Nginx
server block:

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    client_max_body_size 210M;          # >= MAX_PRODUCT_FILE_MB

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;      # /ws
        proxy_set_header   Connection "upgrade";       # /ws
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;                       # long WebSocket idles
    }
}

server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$host$request_uri;
}
```

`Upgrade`/`Connection` are mandatory — without them `/ws` fails and the dashboards fall
back to polling. `client_max_body_size` must exceed `MAX_PRODUCT_FILE_MB`.

**5. First login.** Sign in at `/master` with `DEFAULT_MASTER_USERNAME` /
`DEFAULT_MASTER_PASSWORD`; the app forces a password change immediately (§14). Then add
payment methods, coin packages and products.

**6. Google + email.** Add the production redirect URI (§8) and switch
`EMAIL_BACKEND=smtp` (§9). Send yourself a test purchase and confirm the `.eml`/real mail
arrives with a working download link.

**7. Scaling.** Raise `WEB_CONCURRENCY` (keep `REDIS_URL` set). Move `uploads/` to object
storage or a network volume before running more than one app host — the download route
streams from the local filesystem. Raise Postgres `max_connections` alongside.

**8. Updating.**

```bash
git pull && docker compose up -d --build
```

Migrations run automatically on start. Take a database dump first (§17).

**9. Watch.** `/health` reports app, database and wallet-ledger consistency — point your
uptime monitor at it. `audit_logs` is the security trail; `download_logs` shows every
download attempt and its outcome.

---

# 14. Default Master credentials

Seeded on first startup only, from `.env`:

```text
URL:       /master
Username:  Admin          (DEFAULT_MASTER_USERNAME)
Password:  admin          (DEFAULT_MASTER_PASSWORD)
```

The account is created with `must_change_password = true`. The login succeeds and returns
`must_change_password: true`, and the dashboard forces a rotation through
`POST /api/auth/staff/change-password` before real work can start. Changing the password
revokes every other staff session for that account.

Change `DEFAULT_MASTER_PASSWORD` in `.env` **before** the first production start — the
value is only read while the account is being created, and it is stored as an Argon2id hash,
never in plaintext. Seeding is skipped entirely if a Master already exists, so this cannot
be used to reset a live account.

Sellers do not self-register. A Master creates them (`POST /api/master/sellers`) with a
username, password and per-seller flags (`device_lock`, `can_verify_payments`). A seller's
first login binds their device; a second device is refused until a Master clears the
binding (§15).

---

# 15. Security checklist

Authentication and sessions

- [x] Argon2id password hashing (`argon2-cffi`); no plaintext password is stored or logged
- [x] Sessions are opaque random tokens; only a **hash** is stored, so a database leak does not yield usable cookies
- [x] Separate cookies per surface (`sc_viewer`, `sc_staff`) — a viewer cookie can never authenticate staff
- [x] `HttpOnly` + `SameSite` cookies; `Secure` enforced in production
- [x] Double-submit CSRF: readable `sc_csrf` cookie echoed as `X-CSRF-Token`, validated against the session row on every unsafe method
- [x] Failed-login lockout with `locked_until`, plus a Redis-backed `LOGIN_RATE_LIMIT`
- [x] Password change revokes all other staff sessions for that account
- [x] Logout revokes the session server-side (the cookie alone is worthless afterwards)
- [x] Google OAuth uses a signed single-use `state`, and identity is keyed on the stable `sub`

Device binding

- [x] Server-side device validation, **not** an IP check: an HMAC fingerprint of a client device id + user agent, stored in `devices` and pinned to the session row
- [x] A second device is refused with `403 This account is already bound to another device.`
- [x] Only a Master can clear a binding (`POST /api/master/accounts/{id}/reset-device`), and the reset is audited
- [x] IP addresses are recorded for forensics but never used as the lock

Money — server side only

- [x] Coin credit, deduction, order creation, payment confirmation, refund, delivery and role checks are all server-side; the frontend is never trusted
- [x] Extra fields in a purchase body (`balance`, `coin_price`, …) are ignored by the Pydantic schema
- [x] Append-only `wallet_transactions`; a balance is never written without a ledger row in the same transaction
- [x] `idempotency_key` is UNIQUE — a replay returns the original transaction instead of moving coins again
- [x] The wallet row is locked (`FOR UPDATE` on PostgreSQL) before a balance is read for a mutation
- [x] Payment verification is a conditional `UPDATE … WHERE status='PENDING'`; the second confirm reports `already_processed` and credits nothing
- [x] Uploading a screenshot **never** credits coins; rejection credits nothing
- [x] Refunds are idempotent per order and revoke the order's download grants
- [x] Every manual adjustment requires a reason (≥3 chars) and writes both a ledger row and an audit entry
- [x] Ledger↔cache consistency is asserted by `/health` and by the test suite

Files and downloads

- [x] Download links are per-order, tokenised, expiring (`DOWNLOAD_TOKEN_TTL_HOURS`), attempt-capped (`DOWNLOAD_MAX_ATTEMPTS`) and revocable — never a public permanent URL
- [x] Only a hash of the download token is stored
- [x] A different signed-in user gets `403` and does **not** consume the owner's remaining attempts
- [x] Downloads respond `Cache-Control: no-store` with `Content-Disposition: attachment`
- [x] `uploads/products` and `uploads/screenshots` are never statically served; only `uploads/media` is public
- [x] Payment screenshots are behind a staff-only endpoint
- [x] Uploads are validated by extension allowlist **and** magic bytes; images are decoded with Pillow, so a renamed file is rejected
- [x] Size ceilings are enforced while streaming to disk; stored names are random tokens, and path traversal is blocked by resolving under the upload root
- [x] Every download attempt is logged with outcome, IP and user agent

Transport and application hardening

- [x] Security headers on every response: CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`, plus HSTS in production
- [x] `TrustedHostMiddleware` (`ALLOWED_HOSTS`) and an explicit CORS allowlist
- [x] Rate limits on login, upload, purchase and download-link issuance
- [x] Anonymous WebSocket connections are closed with policy violation `1008`
- [x] `/api/docs` is hidden in production; the dev Google stub is refused in production
- [x] Parameterised queries throughout (SQLAlchemy) — no string-built SQL
- [x] Secrets only in `.env`; `.env` is git-ignored and excluded from the Docker build context
- [x] The container runs as a non-root user
- [x] Full audit trail (`audit_logs`): logins, blocked devices, password changes, account changes, payment decisions, purchases, refunds, catalogue edits

Before going live

- [ ] `SECRET_KEY` replaced with 64 random bytes
- [ ] `ENVIRONMENT=production`, `DEBUG=false`, `COOKIE_SECURE=true`
- [ ] `ALLOWED_HOSTS` / `CORS_ORIGINS` narrowed to your domain
- [ ] `ALLOW_DEV_GOOGLE_STUB=false`
- [ ] Bootstrap Master password rotated
- [ ] `POSTGRES_PASSWORD` changed from the compose default
- [ ] HTTPS terminated in front, HTTP redirected
- [ ] Automated database + `uploads/` backups running (§17)

---

# 16. Testing guide

46 tests. They exercise the **real** ASGI application (real routes, real middleware, real
SQLAlchemy against a temporary SQLite database) — no mocked services, no fake HTTP layer.

```bash
python -m pytest
```

Last run, on the virtualenv `backend/requirements-dev.txt` pins describe (Python 3.14.7):

```text
46 passed, 1 warning in 11.87s
```

The single warning is Starlette deprecating the `HTTP_422_UNPROCESSABLE_ENTITY` constant
name; it comes from inside Starlette's own exception handler, not from this codebase.

| File | Tests | Covers |
|---|---:|---|
| `tests/test_device_lock.py` | 4 | binding on first login, second device refused, Master reset, per-seller opt-out |
| `tests/test_download.py` | 6 | tokenised link, owner binding, attempt ceiling, expiry, revocation on refund, `no-store` |
| `tests/test_payments_idempotency.py` | 4 | upload credits nothing, confirm credits once, double confirm is a no-op, reject credits nothing |
| `tests/test_purchase.py` | 6 | happy path, insufficient coins with shortfall, same-key replay, concurrent double-click, repurchase block, affordability |
| `tests/test_rbac.py` | 10 | anonymous 401s, viewer→staff, seller→master, CSRF enforcement, logout revocation |
| `tests/test_refund.py` | 3 | coins returned, idempotent refund, download grants revoked |
| `tests/test_uploads.py` | 6 | extension allowlist, magic-byte sniffing, forged PNG rejected, size ceiling, staff-only screenshots |
| `tests/test_wallet_ledger.py` | 7 | append-only ledger, balance↔ledger equality, reason required, manual adjust audit, `/health` integrity probe (green **and** red) |

Useful invocations:

| Goal | Command |
|---|---|
| One file | `python -m pytest tests/test_purchase.py -v` |
| One test | `python -m pytest tests/test_purchase.py::test_concurrent_double_click_deducts_once -v` |
| Stop at first failure, full output | `python -m pytest -x -vv` |
| Show print/log output | `python -m pytest -s` |
| Schema drift check | `alembic check` |

`pytest.ini` sets `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed) and session loop
scopes. `tests/conftest.py` sets every environment variable **before** importing `app`,
points `DATABASE_URL` at a throwaway SQLite file under `tests/.tmp/`, boots the real
lifespan once per session, and builds real PNG and ZIP fixtures with Pillow and `zipfile` —
so upload validation and download integrity are tested against genuine files.

Two of the ledger tests are worth calling out because they test the *monitoring*, not just
the code path: one asserts `/health` returns `wallet_ledger.consistent = true` on a healthy
database, and the other writes a balance directly with `UPDATE wallets SET balance = …`,
bypassing the ledger entirely, and asserts the probe goes `ok: false` and names the drifted
wallet. That is the failure the whole ledger design exists to prevent, so it is worth having
a test that proves you would find out.

Beyond the committed suite, the system was validated **over the wire**: a real `uvicorn`
process on a real TCP port against a fresh database, driven through real cookie jars and a
real WebSocket, asserting **123 checks** across infrastructure headers, RBAC, device
binding, payment verification, purchase idempotency, the download ceiling, refunds, ledger
integrity, the audit trail, email delivery and logout. That run finished `123/123 checks
passed`. It was a throwaway harness and is deliberately not checked in — it needed a free
port and a spare process, which makes it a poor fit for CI. `python -m pytest` is the
command to run routinely, and it covers the same invariants in-process.

---

# 17. Backup and restore guide

Two things must be backed up: **PostgreSQL** (everything transactional) and **`uploads/`**
(payment screenshots, product files, outbox mail). Redis needs no backup.

### Back up

```bash
docker compose exec -T db pg_dump -U stream -Fc stream_corporation > "backup-$(date +%F).dump"
```

`uploads/` from the named volume:

```bash
docker run --rm -v stream-corporation_uploads:/data -v "$PWD:/out" alpine tar czf "/out/uploads-$(date +%F).tgz" -C /data .
```

Without Docker: `pg_dump -U stream -Fc stream_corporation > backup.dump` and
`tar czf uploads.tgz uploads/`.

Also keep a copy of `.env` somewhere safe and **outside** git. `SECRET_KEY` is not
recoverable: losing it invalidates every session cookie and every outstanding download
token (customers can re-issue links from `/orders`, so nothing is lost permanently, but
everyone is signed out).

### Restore

```bash
docker compose up -d db && docker compose exec -T db pg_restore -U stream -d stream_corporation --clean --if-exists < backup-2026-08-31.dump
```

Then the uploads:

```bash
docker run --rm -v stream-corporation_uploads:/data -v "$PWD:/in" alpine sh -c "rm -rf /data/* && tar xzf /in/uploads-2026-08-31.tgz -C /data"
```

Finally bring the app up (`docker compose up -d app`) and verify:

```bash
curl -s localhost:8000/health
```

`/health` reports database connectivity **and** wallet-ledger consistency, so a green
response after a restore means balances still equal the sum of their ledger rows. Spot-check
one order's download link too — that proves the `uploads/` restore matched the database.

### Schedule and retention

Nightly `pg_dump` plus a weekly `uploads/` archive is a sensible baseline; keep 7 daily, 4
weekly and 3 monthly copies, store them off-host, and **restore into a scratch database
every so often** — an untested backup is not a backup. Order the steps as: dump → restore →
`alembic upgrade head` → `/health`. Restoring an older dump into a newer codebase is fine;
migrations are forward-only, so the reverse is not.

---

<div align="center">

**STREAM CORPORATION** — coin ledger and order consistency first, everything else second.

</div>
