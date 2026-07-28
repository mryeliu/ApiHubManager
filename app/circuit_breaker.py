"""数据源级熔断器（避坑 #10）。

某源连续失败达阈值即打开，冷却期内其接口快速返回 503，避免雪崩。
状态存 cache（Redis/memory），跨 worker 一致。
"""
import time

from .config import settings
from .cache import cache


async def is_open(source_id: str) -> bool:
    st = await cache.cb_get(source_id)
    if not st.get("open"):
        return False
    if time.time() > st.get("opened_at", 0) + settings.CB_COOLDOWN_SECONDS:
        # 冷却结束，半开重试：清零失败计数器，新一轮从 0 计
        await cache.delete(f"cbfail:{source_id}")
        await cache.cb_set(source_id, {"open": False, "opened_at": 0, "failures": 0})
        return False
    return True


async def record_success(source_id: str) -> None:
    await cache.delete(f"cbfail:{source_id}")
    await cache.cb_set(source_id, {"open": False, "opened_at": 0, "failures": 0})


async def record_failure(source_id: str) -> None:
    # 用原子计数器累加失败数（Redis 下 INCR 原子，消除多 worker read-modify-write 竞态，H-8）；
    # 内存模式下 incr_counter 也是串行累加，单进程内等价。
    # 计数为 1 时（即首次失败）重置状态为关闭，保证每次新故障序列从 0 开始计数。
    failures = await cache.incr_counter(f"cbfail:{source_id}", ttl=0)
    if failures >= settings.CB_FAILURE_THRESHOLD:
        await cache.cb_set(source_id, {"open": True, "opened_at": time.time(), "failures": failures})
    else:
        await cache.cb_set(source_id, {"open": False, "opened_at": 0, "failures": failures})
