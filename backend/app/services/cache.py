from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

try:
    import redis.asyncio as aioredis  # type: ignore
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore

from app.config import settings
from app.utils.logger import log_event

DEFAULT_TTL = 60 * 30  # 30 minutes


def _normalize(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())


class SemanticCache:
    def __init__(self) -> None:
        self._redis = None
        self._mem: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        if aioredis is None:
            return
        try:
            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await self._redis.ping()
        except Exception:
            self._redis = None

    def _key(self, dept: str, query: str) -> str:
        return f"cache:{dept.lower()}:{_normalize(query)}"

    async def get(self, dept: str, query: str) -> Optional[dict]:
        key = self._key(dept, query)
        if self._redis is not None:
            try:
                v = await self._redis.get(key)
                return json.loads(v) if v else None
            except Exception:
                return None
        # in-memory fallback
        import time
        async with self._lock:
            entry = self._mem.get(key)
            if not entry:
                return None
            exp, val = entry
            if exp < time.time():
                self._mem.pop(key, None)
                return None
            return val

    async def set(self, dept: str, query: str, value: dict, ttl: int = DEFAULT_TTL) -> None:
        key = self._key(dept, query)
        if self._redis is not None:
            try:
                await self._redis.set(key, json.dumps(value), ex=ttl)
                return
            except Exception:
                pass
        import time
        async with self._lock:
            self._mem[key] = (time.time() + ttl, value)


cache = SemanticCache()
