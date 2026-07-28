"""异步日志写入器（避坑 #7 / #8 / #22）。

业务请求只投递一条记录到内存队列（不阻塞）；后台任务攒批批量 INSERT 分区表。
采样：错误/慢请求 100% 保留；其余按 qps 阈值决定是否按比例采样（计数不受影响）。
背压：队列满则丢最旧，绝不拖慢业务响应。
"""
import asyncio
import logging
import random
import time

from .config import settings
from .cache import cache
from .metadata_db import SessionLocal
from .models import ApiLog

_queue: asyncio.Queue = asyncio.Queue(maxsize=settings.LOG_QUEUE_MAX)
_running = False


def _get_cfg():
    # 延迟导入，避免与 admin_routes 的循环依赖
    from .admin_routes import _runtime_settings
    return _runtime_settings


async def _should_keep(rec: dict) -> bool:
    status = rec.get("status_code", 0)
    latency = rec.get("latency_ms", 0)
    cfg = _get_cfg()
    slow_ms = cfg.get("slow_ms", settings.SLOW_MS)
    threshold = cfg.get("sample_qps_threshold", settings.SAMPLE_QPS_THRESHOLD)
    ratio = cfg.get("sample_ratio", settings.SAMPLE_RATIO)
    # 错误 / 慢请求 100% 保留（慢阈值随运行时设置变化）
    if status >= 400 or latency > slow_ms:
        return True
    api_id = rec.get("api_id") or "na"
    minute = int(time.time() // 60)
    key = f"logq:{api_id}:{minute}"
    # 采样计数仅当前分钟有意义，设较短 TTL 避免键无限累积（轻微泄漏）
    n = await cache.incr_counter(key, ttl=120)
    # 近似判断高量：本分钟累计 > 阈值*60 → 按比例采样（阈值/比例随运行时设置变化）
    if n > threshold * 60:
        return random.random() < ratio
    return True


async def record(rec: dict) -> None:
    """投递一条日志（脱敏由调用方保证）；决定是否采样后入队。"""
    if not await _should_keep(rec):
        return
    try:
        _queue.put_nowait(rec)
    except asyncio.QueueFull:
        try:
            _queue.get_nowait()  # 丢最旧
        except Exception:
            pass
        try:
            _queue.put_nowait(rec)
        except Exception:
            pass


async def run() -> None:
    """后台批量写入循环。"""
    global _running
    _running = True
    batch: list = []
    while True:
        try:
            rec = await asyncio.wait_for(_queue.get(), timeout=settings.LOG_FLUSH_INTERVAL)
            batch.append(rec)
        except asyncio.TimeoutError:
            pass
        if len(batch) >= settings.LOG_BATCH_SIZE or (batch and _queue.empty()):
            try:
                await _flush(batch)
            except Exception:
                # 后台写入循环绝不能因一次 DB/序列化异常而退出，否则日志永久丢失且不报警
                logging.exception(
                    "日志批量写入失败，本批 %d 条已丢弃（后台写入循环继续）", len(batch)
                )
            batch = []


async def _flush(batch: list) -> None:
    if not batch:
        return
    async with SessionLocal() as s:
        s.add_all([ApiLog(**rec) for rec in batch])
        await s.commit()


def is_running() -> bool:
    return _running
