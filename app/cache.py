"""缓存/协调层封装：Redis（生产）或进程内内存（回退）。

统一异步接口，供路由热更新、实时计数、限流、熔断、结果缓存使用。
内存回退仅用于本地开发/测试，跨进程一致性由 Redis 保证（见避坑 #11/#19）。
"""
import asyncio
import json
import time
from typing import Any, Optional

from .config import settings


class _MemoryBackend:
    """极简进程内 KV，带 TTL；仅开发回退用。"""

    def __init__(self):
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def _get(self, key: str):
        async with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            expire_ts, val = item
            if expire_ts and time.time() > expire_ts:
                self._store.pop(key, None)
                return None
            return val

    async def get(self, key: str) -> Optional[str]:
        return await self._get(key)

    async def set(self, key: str, val: str, ttl: int = 0) -> None:
        async with self._lock:
            self._store[key] = ((time.time() + ttl) if ttl else 0.0, val)

    async def incr(self, key: str, ttl: int = 0) -> int:
        async with self._lock:
            item = self._store.get(key)
            val = 0
            if item:
                expire_ts, old = item
                if not expire_ts or time.time() <= expire_ts:
                    val = int(old)
            val += 1
            self._store[key] = ((time.time() + ttl) if ttl else 0.0, val)
            return val

    async def read(self, key: str) -> Optional[str]:
        async with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            expire_ts, val = item
            if expire_ts and time.time() > expire_ts:
                self._store.pop(key, None)
                return None
            return val

    async def drain(self, key: str) -> int:
        async with self._lock:
            item = self._store.pop(key, None)
            if not item:
                return 0
            expire_ts, val = item
            if expire_ts and time.time() > expire_ts:
                return 0
            return int(val)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)


class Cache:
    def __init__(self):
        self.use_redis = bool(settings.REDIS_URL) and not settings.USE_MEMORY_CACHE
        self._redis = None
        self._mem = _MemoryBackend() if not self.use_redis else None

    async def connect(self) -> None:
        if self.use_redis:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await self._redis.ping()

    # ---- 通用 ----
    async def get(self, key: str) -> Optional[str]:
        if self.use_redis:
            return await self._redis.get(key)
        return await self._mem.get(key)

    async def set(self, key: str, val: str, ttl: int = 0) -> None:
        if self.use_redis:
            await self._redis.set(key, val, ex=ttl or None)
        else:
            await self._mem.set(key, val, ttl)

    async def incr(self, key: str, ttl: int = 0) -> int:
        if self.use_redis:
            # INCR + EXPIRE 原子化：避免进程在两者之间崩溃导致 key 永不过期
            # （限流 key 永久封禁 / 缓存 key 内存泄漏）。
            if ttl:
                lua = (
                    "local n = redis.call('INCR', KEYS[1])\n"
                    "if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end\n"
                    "return n"
                )
                return await self._redis.eval(lua, 1, key, ttl)
            return await self._redis.incr(key)
        return await self._mem.incr(key, ttl)

    async def delete(self, key: str) -> None:
        if self.use_redis:
            await self._redis.delete(key)
        else:
            await self._mem.delete(key)

    # ---- 路由版本（发布热更新，避坑 #5）----
    async def incr_route_version(self) -> int:
        return await self.incr("route:version", ttl=0)

    async def get_route_version(self) -> int:
        v = await self.get("route:version")
        return int(v) if v else 0

    # ---- 实时计数（避坑 #8）----
    async def incr_counter(self, key: str, ttl: int = 0) -> int:
        # ttl 默认 0（不失效），用于 stats 计数（由 daily_stats 周期性 drain）；
        # 采样计数 logq:* 传较短 ttl，避免键无限累积（见 log_writer._should_keep）
        return await self.incr(key, ttl=ttl)

    async def get_counter(self, key: str) -> int:
        """只读读取当前计数，不增减（给概览/趋势用，避免误增）。"""
        if self.use_redis:
            v = await self._redis.get(key)
            return int(v) if v else 0
        v = await self._mem.read(key)
        return int(v) if v is not None else 0

    async def drain_counter(self, key: str) -> int:
        """读取并清零（给落库用，保证 daily_stats 只累加一次）。"""
        if self.use_redis:
            v = await self._redis.getdel(key)
            return int(v) if v else 0
        return await self._mem.drain(key)

    # ---- 限流（固定窗口，跨进程一致，避坑 #11）----
    async def ratelimit_allow(self, key: str, limit: int) -> bool:
        bucket = int(time.time() // 60)  # 每分钟一个桶
        full = f"ratelimit:{key}:{bucket}"
        n = await self.incr(full, ttl=120)
        return n <= limit

    # ---- 熔断（数据源级，避坑 #10）----
    async def cb_get(self, source_id: str) -> dict:
        raw = await self.get(f"cb:{source_id}")
        return json.loads(raw) if raw else {"open": False, "opened_at": 0, "failures": 0}

    async def cb_set(self, source_id: str, state: dict) -> None:
        await self.set(f"cb:{source_id}", json.dumps(state), ttl=0)

    # ---- 结果缓存（避坑 #15）----
    async def cache_get(self, key: str) -> Optional[str]:
        return await self.get(f"cache:{key}")

    async def cache_set(self, key: str, val: str, ttl: int) -> None:
        await self.set(f"cache:{key}", val, ttl)


cache = Cache()
