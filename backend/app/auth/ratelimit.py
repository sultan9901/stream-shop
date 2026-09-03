"""Sliding-window rate limiting (Redis-backed, in-process fallback)."""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.auth.sessions import client_ip
from app.config import settings
from app.services.cache import get_redis

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SPEC = re.compile(r"^(\d+)\s*/\s*(\d*)([smhd])$", re.I)

_local: dict[str, deque[float]] = defaultdict(deque)


def parse_spec(spec: str) -> tuple[int, int]:
    """``"10/5m"`` -> ``(10, 300)``."""
    m = _SPEC.match((spec or "").strip())
    if not m:
        return 60, 60
    limit, qty, unit = m.group(1), m.group(2), m.group(3).lower()
    window = (int(qty) if qty else 1) * _UNITS[unit]
    return int(limit), window


async def hit(key: str, spec: str) -> tuple[bool, int]:
    """Register one hit. Returns ``(allowed, retry_after_seconds)``."""
    if not settings.rate_limit_enabled:
        return True, 0
    limit, window = parse_spec(spec)
    now = time.time()

    redis = await get_redis()
    if redis is not None:
        try:
            bucket = f"rl:{key}"
            pipe = redis.pipeline()
            pipe.zremrangebyscore(bucket, 0, now - window)
            pipe.zadd(bucket, {f"{now:.6f}:{id(now)}": now})
            pipe.zcard(bucket)
            pipe.expire(bucket, window + 5)
            _, _, count, _ = await pipe.execute()
            if int(count) > limit:
                return False, window
            return True, 0
        except Exception:
            pass  # fall through to local

    dq = _local[key]
    cutoff = now - window
    while dq and dq[0] < cutoff:
        dq.popleft()
    dq.append(now)
    if len(dq) > limit:
        return False, int(window - (now - dq[0])) + 1
    return True, 0


async def enforce(request: Request, bucket: str, spec: str, *, extra: str = "") -> None:
    """Raise 429 when the caller exceeds ``spec`` for ``bucket``."""
    ident = extra or client_ip(request)
    allowed, retry = await hit(f"{bucket}:{ident}", spec)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please slow down and try again shortly.",
            headers={"Retry-After": str(max(retry, 1))},
        )


def clear_local() -> None:
    _local.clear()
