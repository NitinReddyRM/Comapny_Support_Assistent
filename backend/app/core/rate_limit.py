"""
Token-bucket rate limiter using Redis (or in-memory fallback in dev).

We key by user_id (when authenticated) or remote-addr. Designed for
per-route throttling: callers do `await rl.check(key, limit, window)`.
"""
import asyncio
import time
from typing import Optional

try:
    import redis.asyncio as aioredis  # type: ignore
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore

from app.config import settings
from app.core.exceptions import RateLimited


class RateLimiter:
    def __init__(self) -> None:
        self._redis: Optional["aioredis.Redis"] = None
        self._memory: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        if aioredis is None:
            return
        try:
            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await self._redis.ping()
        except Exception:
            self._redis = None

    async def check(self, key: str, limit: int, window_seconds: int = 60) -> None:
        """Raise RateLimited if `key` has exceeded `limit` within window."""
        
        bucket = f"rl:{key}:{window_seconds}"
        now = time.time()
        print("&"*60)
        print(key,limit,window_seconds)
        print("&"*70)
        if self._redis is not None:
            pipe = self._redis.pipeline()
            pipe.incr(bucket, 1)
            pipe.expire(bucket, window_seconds)
            count, _ = await pipe.execute()
            print("&"*60)
            print("not exceed")
            print("&"*70)
            if int(count) > limit:
                raise RateLimited()
            return
        print("&"*60)
        print("In Rate Limiter")
        print("&"*70)
        # In-memory fallback
        async with self._lock:
            arr = self._memory.setdefault(bucket, [])
            arr[:] = [t for t in arr if now - t < window_seconds]
            if len(arr) >= limit:
                raise RateLimited()
            print("&"*60)
            print(arr)
            print("&"*70)
            arr.append(now)


limiter = RateLimiter()
