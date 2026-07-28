"""限流器（跨进程一致，避坑 #11）。

单 IP + 单接口两个维度，固定窗口计数走 Redis/memory（cache 封装）。
返回 True 表示允许；False 表示触发限流（调用方返回 429）。
"""
from .config import settings
from .cache import cache


async def allow(ip: str, api_id: str) -> bool:
    if not settings.RL_ENABLED:
        return True
    ip_ok = await cache.ratelimit_allow(f"ip:{ip}", settings.RL_IP_LIMIT)
    api_ok = await cache.ratelimit_allow(f"api:{api_id}", settings.RL_API_LIMIT)
    return ip_ok and api_ok
