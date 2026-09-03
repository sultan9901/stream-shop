# ==========================================================================
# STREAM CORPORATION — production image.
#
#   docker build -t stream-corporation .
#   docker run --env-file .env -p 8000:8000 stream-corporation
#
# Two stages: the builder compiles wheels into a self-contained virtualenv,
# the runtime carries only that virtualenv plus the application source. No
# compiler, no pip cache and no build headers reach the final image.
#
# The image runs as the unprivileged user `stream` (uid 10001). It never
# contains a `.env` file — configuration arrives at run time through
# `--env-file` / compose `env_file:` / your orchestrator's secret store.
# ==========================================================================

# --------------------------------------------------------------- build stage
FROM python:3.14-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build toolchain for any dependency that has no manylinux wheel for this
# interpreter (argon2-cffi -> cffi, asyncpg, greenlet, pillow).
RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
        libpq-dev \
        libjpeg62-turbo-dev \
        zlib1g-dev \
        libfreetype6-dev \
        libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r /tmp/requirements.txt

# ------------------------------------------------------------- runtime stage
FROM python:3.14-slim-bookworm AS runtime

# Runtime shared libraries only (no -dev packages, no compiler).
# postgresql-client provides `pg_isready`, used by the entrypoint to wait for
# the database before running migrations.
RUN apt-get update && apt-get install --no-install-recommends -y \
        libpq5 \
        libjpeg62-turbo \
        libwebp7 \
        libfreetype6 \
        postgresql-client \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 stream \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin stream

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    WEB_CONCURRENCY=2 \
    RUN_MIGRATIONS=true \
    UPLOAD_DIR=/app/uploads

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Source, migrations and templates. Ownership is set at copy time so no
# recursive chown layer is needed.
COPY --chown=stream:stream backend/     /app/backend/
COPY --chown=stream:stream frontend/    /app/frontend/
COPY --chown=stream:stream migrations/  /app/migrations/
COPY --chown=stream:stream alembic.ini  /app/alembic.ini
COPY --chown=stream:stream docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/uploads/screenshots /app/uploads/products /app/uploads/media /app/uploads/outbox \
    && chown -R stream:stream /app/uploads

# Screenshots, product files and outbox mail must survive a container replace.
VOLUME ["/app/uploads"]

USER stream
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${APP_PORT}/health" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
