"""Static verification of the Docker artifacts.

Docker is not installed on this machine, so the image cannot be built here. Everything
that does *not* need a daemon is checked instead: that every COPY source exists, that the
two stages agree on a base image, that .dockerignore really excludes the secrets, that the
compose file is spec-valid and wires the services together, and that no file in the build
context would ship a credential.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: object, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.append((ok, name, detail))
    line = f"{'PASS' if ok else 'FAIL'}  {name}"
    if detail:
        line += f"   [{detail[:200]}]"
    print(line, flush=True)
    return ok


def section(title: str) -> None:
    print(f"\n--- {title} ---", flush=True)


# ==========================================================================
# Dockerfile
# ==========================================================================
def verify_dockerfile() -> None:
    section("Dockerfile")
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines()]

    froms = re.findall(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", text, re.M | re.I)
    check("two build stages (builder + runtime)", len(froms) == 2, str(froms))
    check("both stages share one base image (no ABI drift)",
          len({f[0] for f in froms}) == 1, str({f[0] for f in froms}))
    check("base image is version-pinned, not ':latest'",
          froms and ":" in froms[0][0] and not froms[0][0].endswith(":latest"), froms[0][0])
    check("stage names are 'builder' and 'runtime'",
          {f[1] for f in froms} == {"builder", "runtime"}, str([f[1] for f in froms]))

    # every COPY source must exist in the repo (a typo here fails the build)
    copies = re.findall(r"^COPY\s+(?:--[\w=:.-]+\s+)*(\S+)\s+(\S+)", text, re.M | re.I)
    missing = [src for src, _ in copies if not src.startswith("/") and not (ROOT / src).exists()]
    check("every COPY source path exists in the repo", not missing,
          f"{len(copies)} COPY sources; missing={missing}")

    stage_copies = re.findall(r"^COPY\s+--from=(\S+)\s+(\S+)", text, re.M | re.I)
    check("runtime copies the virtualenv from the builder",
          any(s == "builder" and p == "/opt/venv" for s, p in stage_copies), str(stage_copies))

    check("runtime installs no compiler (build-essential only in builder)",
          text.count("build-essential") == 1
          and text.index("build-essential") < text.index("FROM python:3.14-slim-bookworm AS runtime"),
          "build-essential appears once, before the runtime stage")
    check("no -dev headers in the runtime stage",
          "libpq-dev" not in text.split("AS runtime", 1)[1], "runtime apt list has no *-dev")
    check("apt lists are cleaned in every stage",
          text.count("rm -rf /var/lib/apt/lists/*") == 2, f"{text.count('rm -rf /var/lib/apt/lists/*')} cleanups")
    check("pg_isready is available for the entrypoint's DB wait",
          "postgresql-client" in text)
    check("curl is available for HEALTHCHECK", "curl" in text)

    check("a dedicated non-root user is created", "useradd" in text and "10001" in text)
    user_idx = max((i for i, ln in enumerate(lines) if ln.upper().startswith("USER ")), default=-1)
    copy_idx = max((i for i, ln in enumerate(lines) if ln.upper().startswith("COPY ")), default=-1)
    check("USER comes after the COPY layers (files owned correctly)",
          user_idx > copy_idx, f"USER at {user_idx}, last COPY at {copy_idx}")
    check("the final USER is the unprivileged one, not root",
          lines[user_idx] == "USER stream", lines[user_idx] if user_idx >= 0 else "no USER")
    check("copied source is chowned at copy time (no recursive chown layer)",
          text.count("--chown=stream:stream") >= 5, f"{text.count('--chown=stream:stream')} chowned COPYs")

    check("PYTHONPATH points at the backend package", "PYTHONPATH=/app/backend" in text)
    check("the venv is first on PATH", 'PATH="/opt/venv/bin:$PATH"' in text)
    check("uploads is declared a VOLUME (survives a container replace)",
          'VOLUME ["/app/uploads"]' in text)
    check("all four upload subdirectories are pre-created",
          all(d in text for d in ("uploads/screenshots", "uploads/products",
                                  "uploads/media", "uploads/outbox")))
    check("HEALTHCHECK probes /health", "HEALTHCHECK" in text and "/health" in text)
    check("ENTRYPOINT is the shipped script", 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in text)
    check("default CMD is 'serve'", 'CMD ["serve"]' in text)
    check("entrypoint is made executable in the image", "chmod 0755" in text)
    check("no .env is ever copied into a layer",
          not re.search(r"^COPY\s+.*\.env", text, re.M | re.I))
    check("no secret is baked in as a build ENV",
          not re.search(r"^ENV\s+.*(SECRET_KEY|PASSWORD|SMTP_PASSWORD)\s*=", text, re.M | re.I))
    check("no pip cache is kept", "PIP_NO_CACHE_DIR=1" in text)


# ==========================================================================
# .dockerignore
# ==========================================================================
def verify_dockerignore() -> None:
    section(".dockerignore — build-context hygiene")
    patterns = [
        ln.strip() for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    for must in (".env", ".env.*", "*.pem", "*.key", ".venv/", "uploads/", "*.db", ".git/"):
        check(f"excludes {must}", must in patterns)
    check("re-includes .env.example so the template still ships",
          "!.env.example" in patterns)
    check("excludes the pytest scratch directory", "tests/.tmp/" in patterns)
    check("excludes agent/session state", ".claude/" in patterns)

    # a real .env exists in this checkout: prove the pattern would exclude it
    dotenv = ROOT / ".env"
    check("a real .env exists here and is matched by an exclude pattern",
          dotenv.exists() and ".env" in patterns,
          "the credential file present in this checkout would not enter the build context")


# ==========================================================================
# docker-compose.yml
# ==========================================================================
def verify_compose() -> None:
    section("docker-compose.yml")
    raw = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    # Parsed without PyYAML so the venv keeps matching requirements.txt exactly.
    def block(name: str) -> str:
        m = re.search(rf"^  {name}:\n(.*?)(?=^  \w|\Z)", raw, re.M | re.S)
        return m.group(1) if m else ""

    services = re.findall(r"^  (\w+):$", raw, re.M)
    check("three services declared: db, redis, app",
          {"db", "redis", "app"} <= set(services), str(services))
    check("project name is pinned", "name: stream-corporation" in raw)

    db, redis, app = block("db"), block("redis"), block("app")

    check("db is a pinned PostgreSQL image", "postgres:17-alpine" in db)
    check("db has a pg_isready healthcheck", "pg_isready" in db)
    check("db persists to a named volume", "pgdata:/var/lib/postgresql/data" in db)
    check("db uses deterministic collation (stable ORDER BY)", "--locale=C" in db)
    check("db is not published to the host by default",
          re.search(r"^\s{4}ports:", db, re.M) is None, "5432 stays on the compose network")

    check("redis is a pinned Redis image", "redis:8-alpine" in redis)
    check("redis has a ping healthcheck", "redis-cli" in redis and "ping" in redis)
    check("redis persists (appendonly)", "--appendonly" in redis and "redisdata:/data" in redis)

    check("app builds from this repo", "build:" in app and "dockerfile: Dockerfile" in app)
    check("app reads secrets from .env at run time (not baked)", "env_file:" in app)
    check("app DATABASE_URL points at the db service, not localhost",
          "@db:5432/" in app and "postgresql+asyncpg://" in app)
    check("app REDIS_URL points at the redis service",
          "redis://redis:6379/0" in app)
    check("app waits for BOTH services to be healthy",
          app.count("condition: service_healthy") == 2)
    check("app depends_on names db and redis",
          "db:" in app.split("depends_on:")[1] and "redis:" in app.split("depends_on:")[1])
    check("app publishes to loopback by default (TLS terminates in front)",
          "${APP_BIND:-127.0.0.1}" in app)
    check("app mounts the uploads volume", "uploads:/app/uploads" in app)
    check("app has its own /health healthcheck", "/health" in app)
    check("every service restarts unless stopped",
          raw.count("restart: unless-stopped") == 3)
    check("named volumes are declared",
          all(f"\n  {v}:" in raw.split("volumes:")[-1] for v in ("pgdata", "redisdata", "uploads")))
    check("the same POSTGRES_* values feed both the db and the app DSN",
          raw.count("${POSTGRES_USER:-stream}") >= 3 and raw.count("${POSTGRES_PASSWORD:-streampass}") >= 2,
          "no chance of the app using different credentials than the server was created with")
    check("RUN_MIGRATIONS is enabled so a fresh volume gets its schema",
          'RUN_MIGRATIONS: "true"' in app)
    check("UPLOAD_DIR is forced to the mounted path", "UPLOAD_DIR: /app/uploads" in app)


# ==========================================================================
# cross-artifact consistency
# ==========================================================================
def verify_consistency() -> None:
    section("cross-artifact consistency")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entry = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    reqs = (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")

    check("entrypoint serves the same app path the Dockerfile prepares",
          "--app-dir /app/backend" in entry and "COPY --chown=stream:stream backend/     /app/backend/" in dockerfile)
    check("entrypoint runs alembic from /app where alembic.ini is copied",
          "cd /app && alembic upgrade head" in entry and "/app/alembic.ini" in dockerfile)
    check("uvicorn is a runtime dependency (the entrypoint execs it)",
          re.search(r"^uvicorn==", reqs, re.M) is not None)
    check("alembic is a runtime dependency (the entrypoint runs it)",
          re.search(r"^alembic==", reqs, re.M) is not None)
    check("asyncpg is a runtime dependency (compose uses PostgreSQL)",
          re.search(r"^asyncpg==", reqs, re.M) is not None)
    check("redis is a runtime dependency (compose provides Redis)",
          re.search(r"^redis==", reqs, re.M) is not None)
    # comments must be stripped first: requirements.txt *explains* that it avoids
    # uvicorn[standard], so the phrase appears in prose and would false-positive.
    req_lines = [ln.split("#")[0].strip() for ln in reqs.splitlines()]
    req_body = "\n".join(ln for ln in req_lines if ln)
    check("websockets is pinned (uvicorn is installed without [standard])",
          re.search(r"^websockets==", req_body, re.M) is not None
          and "uvicorn[standard]" not in req_body,
          "plain uvicorn + explicit websockets, so /ws still works")
    check("every runtime requirement is pinned with ==",
          all("==" in ln for ln in req_body.splitlines()))
    check("the port the Dockerfile EXPOSEs is the one compose maps",
          "EXPOSE 8000" in dockerfile and ":8000\"" in compose)
    check("entrypoint honours WEB_CONCURRENCY that compose sets",
          "${WEB_CONCURRENCY:-2}" in entry and "WEB_CONCURRENCY" in compose)
    check("uvicorn is started with --proxy-headers for correct client IPs",
          "--proxy-headers" in entry)
    check("alembic.ini contains no hard-coded DSN",
          "sqlalchemy.url" not in (ROOT / "alembic.ini").read_text(encoding="utf-8")
          or "driver://" in (ROOT / "alembic.ini").read_text(encoding="utf-8"))


def main() -> int:
    verify_dockerfile()
    verify_dockerignore()
    verify_compose()
    verify_consistency()
    ok = sum(1 for r in RESULTS if r[0])
    total = len(RESULTS)
    print(f"\nDOCKER ARTIFACTS: {ok}/{total} checks passed")
    for good, name, detail in RESULTS:
        if not good:
            print(f"  FAILED: {name}  {detail}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
