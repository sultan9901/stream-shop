"""Async SQLAlchemy engine / session plumbing.

Design notes
------------
* One async engine for the whole process.
* ``SELECT ... FOR UPDATE`` is used for wallet mutations. On PostgreSQL this is a
  real row lock; SQLite serialises writers at the file level, so both backends are
  safe. Money-critical paths *additionally* rely on unique idempotency keys so
  correctness never depends on lock semantics alone.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.models.base import Base

log = logging.getLogger(__name__)

_connect_args: dict[str, Any] = {}
_engine_kwargs: dict[str, Any] = {
    "echo": False,
    "future": True,
    "pool_pre_ping": True,
}

if settings.is_sqlite:
    # allow the same connection to be used from the threadpool executor
    _connect_args["check_same_thread"] = False
    _connect_args["timeout"] = 30
else:
    _engine_kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)

engine: AsyncEngine = create_async_engine(
    settings.database_url, connect_args=_connect_args, **_engine_kwargs
)


# --- SQLite writer serialisation -------------------------------------------------
# SQLite allows exactly one writer, and its ``busy_timeout`` is *not* honoured when
# the blocking connection lives in the same process — SQLite returns SQLITE_BUSY
# immediately to sidestep a deadlock. WAL keeps readers non-blocking, so only
# writer-vs-writer collisions fail (a background delivery task racing a request
# handler, say). This process-wide lock serialises those writers so no two SQLite
# connections ever hold the write lock at once. It is held only for the actual
# write span (first flush → commit), never across a whole request, so a request's
# background task can never deadlock against the request that scheduled it. On
# PostgreSQL — real cross-process row locking — every hook below is a no-op.
sqlite_write_lock: asyncio.Lock = asyncio.Lock()


class _WriterSerializedSession(AsyncSession):
    """AsyncSession that grabs :data:`sqlite_write_lock` on its first writing flush
    and releases it at commit/rollback/close, serialising SQLite writers."""

    _holds_write_lock: bool = False

    async def _acquire_writer(self) -> None:
        if settings.is_sqlite and not self._holds_write_lock:
            await sqlite_write_lock.acquire()
            self._holds_write_lock = True

    def _release_writer(self) -> None:
        if self._holds_write_lock:
            sqlite_write_lock.release()
            self._holds_write_lock = False

    async def flush(self, objects=None) -> None:
        # a flush emits INSERT/UPDATE/DELETE and takes SQLite's write lock
        await self._acquire_writer()
        await super().flush(objects)

    async def execute(self, statement, *args, **kwargs):
        # Core DML (`update()`/`insert()`/`delete()`) goes straight to the
        # connection without a flush, so intercept it here too. `scalar()`/
        # `scalars()` both route through `execute()`, so this covers them.
        if settings.is_sqlite and getattr(statement, "is_dml", False):
            await self._acquire_writer()
        return await super().execute(statement, *args, **kwargs)

    async def commit(self) -> None:
        # commit flushes any pending writes; own the lock across the whole commit
        await self._acquire_writer()
        try:
            await super().commit()
        finally:
            self._release_writer()

    async def rollback(self) -> None:
        try:
            await super().rollback()
        finally:
            self._release_writer()

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            self._release_writer()


SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
    class_=_WriterSerializedSession,
)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver level
    """Enable FK enforcement + WAL so SQLite behaves like a real database."""
    if not settings.is_sqlite:
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, rolled back on error."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create the schema on a *fresh* database, and get out of Alembic's way otherwise.

    Two things have to be true at once:

    * ``uvicorn`` alone must work for local development — no one should have to run
      Alembic before the first ``GET /``.
    * Alembic must stay authoritative. A schema built by ``create_all`` carries no
      ``alembic_version`` row, so the database would look un-migrated forever:
      ``alembic check`` reports "not up to date" and a later ``alembic upgrade head``
      tries to re-create tables that already exist.

    So: an empty database is created *and stamped at head*; a database that already
    has tables is left completely alone, because at that point either Alembic owns it
    or a previous boot already stamped it.
    """
    # import for side effects: registers every mapper on Base.metadata
    import app.models  # noqa: F401

    async with engine.begin() as conn:
        existing = set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))
        # `alembic_version` alone is not a schema — a bare `alembic current` creates it.
        if existing & set(Base.metadata.tables):
            return
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_stamp_alembic_head)


def _stamp_alembic_head(sync_conn) -> None:
    """Record the current migration head for a schema we just built with ``create_all``.

    Best-effort by design: if ``alembic.ini`` is not reachable from the working
    directory the app must still boot, so a failure here is logged and swallowed
    rather than raised.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parents[2]  # backend/app/database.py -> repo root
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "migrations"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
        if not head:
            return
        sync_conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        sync_conn.execute(text("DELETE FROM alembic_version"))
        sync_conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head})
        log.info("fresh database created by create_all; stamped alembic_version at %s", head)
    except Exception as exc:  # pragma: no cover - never block startup over bookkeeping
        log.warning("could not stamp alembic_version on the new database: %s", exc)


async def healthcheck() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def dispose_db() -> None:
    await engine.dispose()
