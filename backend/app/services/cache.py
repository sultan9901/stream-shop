"""Redis connection helper with a graceful in-process fallback.

Redis is optional: when ``REDIS_URL`` is empty (or the server is unreachable) the
app keeps working with per-process rate limiting and WebSocket fan-out. Set
``REDIS_URL`` in production so limits and notifications are shared across workers.
"""
from __future__ import annotations

import logging

from app.config import settings

log = logging.getLogger("stream.cache")

_client = None
_checked = False


async def get_redis():
    """Return a connected redis client, or ``None`` if unavailable."""
    global _client, _checked
    if _client is not None:
        return _client
    if _checked or not settings.redis_url:
        return None
    _checked = True
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=3, socket_timeout=3,
        )
        await client.ping()
        _client = client
        log.info("redis connected: %s", settings.redis_url)
        return _client
    except Exception as exc:  # pragma: no cover - depends on environment
        log.warning("redis unavailable (%s) — using in-process fallback", exc)
        return None


async def close_redis() -> None:
    global _client, _checked
    if _client is not None:
        try:
            await _client.close()
        except Exception:  # pragma: no cover
            pass
    _client = None
    _checked = False


def reset_probe() -> None:
    """Allow another connection attempt (used by tests)."""
    global _checked
    _checked = False
