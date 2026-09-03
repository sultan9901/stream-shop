"""WebSocket hub with optional Redis fan-out across workers.

Topics
------
``user:<id>``   – a single account (viewer, seller or master)
``role:MASTER`` – every connected master
``role:SELLER`` – every connected seller
``seller:<id>`` – the seller assigned to a product/order
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

from app.services.cache import get_redis

log = logging.getLogger("stream.ws")
CHANNEL = "sc:events"


class ConnectionManager:
    def __init__(self) -> None:
        self._topics: dict[str, set[WebSocket]] = defaultdict(set)
        self._meta: dict[WebSocket, set[str]] = {}
        self._lock = asyncio.Lock()
        self._pubsub_task: asyncio.Task | None = None

    # ---------------- lifecycle ----------------
    async def connect(self, ws: WebSocket, topics: list[str]) -> None:
        await ws.accept()
        async with self._lock:
            self._meta[ws] = set(topics)
            for t in topics:
                self._topics[t].add(ws)
        await self._send(ws, {"type": "connected", "topics": topics})

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            for t in self._meta.pop(ws, set()):
                self._topics[t].discard(ws)
                if not self._topics[t]:
                    self._topics.pop(t, None)

    def stats(self) -> dict:
        return {
            "connections": len(self._meta),
            "topics": {t: len(s) for t, s in self._topics.items()},
        }

    # ---------------- publishing ----------------
    async def broadcast(self, topics: list[str], event: dict) -> None:
        """Deliver ``event`` to every socket subscribed to any of ``topics``."""
        redis = await get_redis()
        if redis is not None:
            try:
                await redis.publish(CHANNEL, json.dumps({"topics": topics, "event": event}))
                return  # the local subscriber loop will deliver to this worker too
            except Exception:  # pragma: no cover
                log.debug("redis publish failed; delivering locally")
        await self.deliver_local(topics, event)

    async def deliver_local(self, topics: list[str], event: dict) -> None:
        targets: set[WebSocket] = set()
        async with self._lock:
            for t in topics:
                targets |= set(self._topics.get(t, ()))
        if not targets:
            return
        dead: list[WebSocket] = []
        for ws in targets:
            if not await self._send(ws, event):
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    async def _send(self, ws: WebSocket, payload: dict) -> bool:
        try:
            await ws.send_text(json.dumps(payload, default=str))
            return True
        except Exception:
            return False

    # ---------------- redis subscriber ----------------
    async def start_pubsub(self) -> None:
        redis = await get_redis()
        if redis is None or self._pubsub_task is not None:
            return
        self._pubsub_task = asyncio.create_task(self._pubsub_loop(redis))

    async def _pubsub_loop(self, redis) -> None:  # pragma: no cover - needs redis
        pubsub = redis.pubsub()
        await pubsub.subscribe(CHANNEL)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    await self.deliver_local(data.get("topics", []), data.get("event", {}))
                except Exception:
                    log.exception("bad pubsub payload")
        except asyncio.CancelledError:
            pass
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(CHANNEL)
                await pubsub.close()

    async def shutdown(self) -> None:
        if self._pubsub_task:
            self._pubsub_task.cancel()
            with contextlib.suppress(Exception):
                await self._pubsub_task
            self._pubsub_task = None
        async with self._lock:
            sockets = list(self._meta)
        for ws in sockets:
            with contextlib.suppress(Exception):
                await ws.close()
            await self.disconnect(ws)


manager = ConnectionManager()


def topics_for(role: str, user_id: str) -> list[str]:
    topics = [f"user:{user_id}", f"role:{role}"]
    if role == "SELLER":
        topics.append(f"seller:{user_id}")
    return topics
