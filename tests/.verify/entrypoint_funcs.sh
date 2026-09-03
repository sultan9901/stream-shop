#!/usr/bin/env bash
# ==========================================================================
# STREAM CORPORATION — container entrypoint.
#
#   serve            (default) wait for the DB, migrate, then run uvicorn
#   migrate          run `alembic upgrade head` and exit
#   shell            interactive python with the app importable
#   <anything else>  exec'd verbatim, so `docker run ... bash` still works
#
# Migrations run here rather than in the app process: with WEB_CONCURRENCY > 1
# several workers would otherwise race to upgrade the same schema.
# ==========================================================================
set -euo pipefail

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# --------------------------------------------------------------------------
# 1. Refuse to start with a configuration that is unsafe in production.
# --------------------------------------------------------------------------
validate_config() {
    if [[ "${ENVIRONMENT:-development}" != "production" ]]; then
        return 0
    fi
    local fatal=0
    if [[ -z "${SECRET_KEY:-}" || ${#SECRET_KEY} -lt 32 ]]; then
        log "FATAL: SECRET_KEY must be set to at least 32 characters in production."
        fatal=1
    fi
    if [[ "${COOKIE_SECURE:-}" != "true" ]]; then
        log "FATAL: COOKIE_SECURE must be true in production (cookies would be sent over plain HTTP)."
        fatal=1
    fi
    if [[ "${ALLOWED_HOSTS:-*}" == "*" ]]; then
        log "FATAL: ALLOWED_HOSTS must name your real domain(s) in production, not '*'."
        fatal=1
    fi
    if [[ "${DEFAULT_MASTER_PASSWORD:-}" == "admin" ]]; then
        log "WARNING: DEFAULT_MASTER_PASSWORD is still 'admin'. The first login forces a"
        log "         change, but set a real bootstrap password anyway."
    fi
    if [[ "${DATABASE_URL:-}" == sqlite* ]]; then
        log "WARNING: DATABASE_URL points at SQLite. Use PostgreSQL for production."
    fi
    if [[ "${WEB_CONCURRENCY:-2}" -gt 1 && -z "${REDIS_URL:-}" ]]; then
        log "WARNING: WEB_CONCURRENCY=${WEB_CONCURRENCY:-2} with an empty REDIS_URL. Rate-limit"
        log "         counters and WebSocket broadcasts are then per-worker, so a live"
        log "         notification only reaches the clients attached to the worker that"
        log "         produced it. Set REDIS_URL, or run a single worker."
    fi
    [[ $fatal -eq 0 ]] || exit 78   # EX_CONFIG
}

# --------------------------------------------------------------------------
# 2. Wait for PostgreSQL, if that is what DATABASE_URL points at.
# --------------------------------------------------------------------------
wait_for_db() {
    local url="${DATABASE_URL:-}"
    [[ "$url" == postgres* ]] || return 0

    # postgresql+asyncpg://user:pass@host:port/name -> host / port
    local hostport="${url#*@}"; hostport="${hostport%%/*}"
    local host="${hostport%%:*}"
    local port="${hostport##*:}"
    [[ "$port" == "$host" ]] && port=5432

    log "waiting for postgres at ${host}:${port} ..."
    for _ in $(seq 1 60); do
        if pg_isready --quiet --host="$host" --port="$port" 2>/dev/null; then
            log "postgres is accepting connections"
            return 0
        fi
        sleep 1
    done
    log "FATAL: postgres at ${host}:${port} never became ready."
    exit 75   # EX_TEMPFAIL — let the orchestrator restart us
}

run_migrations() {
    if [[ "${RUN_MIGRATIONS:-true}" != "true" ]]; then
        log "RUN_MIGRATIONS is not 'true' — skipping alembic."
        return 0
    fi
    log "applying database migrations (alembic upgrade head)"
    cd /app && alembic upgrade head
    log "schema is at head"
}

