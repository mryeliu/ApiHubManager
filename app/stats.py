"""统计与分区维护（避坑 #8 / #16）。

- Redis 实时计数周期性 flush 到 daily_stats（原子累加）
- api_logs 按天分区：预建次日分区 + DROP 超过留存的天（绝不用 DELETE）
"""
import time as _time
import math
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select, func, text, delete, case

from .config import settings
from .cache import cache
from .metadata_db import SessionLocal, engine, IS_MYSQL
from .models import ApiDefinition, DataSource, DailyStat, ApiLog

_START = _time.time()  # 进程启动时刻，用于「运行时长」


def _get_cfg():
    # 延迟导入，避免与 admin_routes 的循环依赖
    from .admin_routes import _runtime_settings
    return _runtime_settings


async def _flush_one(api_id: str, day: str) -> None:
    # 读取并清零，保证 daily_stats 只累加一次（落库 delta）
    calls = await cache.drain_counter(f"stats:calls:{api_id}:{day}")
    errors = await cache.drain_counter(f"stats:errors:{api_id}:{day}")
    if calls == 0 and errors == 0:
        return
    async with SessionLocal() as s:
        row = (await s.execute(
            select(DailyStat).where(DailyStat.api_id == api_id, DailyStat.day == day)
        )).scalar_one_or_none()
        if row:
            row.calls += calls
            row.errors += errors
        else:
            s.add(DailyStat(api_id=api_id, day=day, calls=calls, errors=errors))
        await s.commit()


async def flush_daily_stats() -> None:
    """后台周期任务：把当日计数落 daily_stats（含 404/405 等 miss 桶）。"""
    today = date.today()  # date 对象，daily_stats.day 为 Date 列
    async with SessionLocal() as s:
        ids = (await s.execute(select(ApiDefinition.id))).scalars().all()
    for aid in ids:
        await _flush_one(aid, today)
    await _flush_one("", today)  # miss 桶（无 entry 的调用）


async def _today_log_counts() -> tuple[int, int]:
    """今日调用数 / 错误数直接从 api_logs 统计（单一事实源，与日志列表一致）。

    不再依赖易失计数器：避免 dev 模式重启丢失内存计数、以及高 QPS 采样丢日志
    导致的「调用数 ≠ 日志数」偏差。
    """
    today_start = datetime.combine(date.today(), time.min)
    async with SessionLocal() as s:
        calls = (await s.execute(
            select(func.count()).select_from(ApiLog).where(ApiLog.log_date >= today_start)
        )).scalar() or 0
        errors = (await s.execute(
            select(func.count()).select_from(ApiLog).where(
                ApiLog.log_date >= today_start, ApiLog.status_code >= 400)
        )).scalar() or 0
    return int(calls), int(errors)


async def _trend_from_logs(api_id, days: int, start_date: date) -> list:
    """从 api_logs 按天聚合调用/错误趋势（单一事实源，与日志列表一致）。

    替代旧的 DailyStat 双源：旧逻辑依赖后台每 60s flush，进程在 60s 内重启会留下 0 点，
    与「今日 api_logs」统计不一致。现统一从 api_logs 聚合，重启/采样均不影响。
    """
    from sqlalchemy import case as _case
    cutoff = datetime.combine(start_date, time.min)
    async with SessionLocal() as s:
        day_expr = func.date(ApiLog.log_date)
        stmt = (select(day_expr, func.count(),
                       func.coalesce(func.sum(_case((ApiLog.status_code >= 400, 1), else_=0)), 0))
                .select_from(ApiLog).where(ApiLog.log_date >= cutoff))
        if api_id:
            stmt = stmt.where(ApiLog.api_id == api_id)
        stmt = stmt.group_by(day_expr)
        rows = (await s.execute(stmt)).all()
    by_day = {str(r[0]): {"calls": int(r[1] or 0), "errors": int(r[2] or 0)} for r in rows}
    out = []
    for i in range(days):
        d = (start_date + timedelta(days=i)).isoformat()
        rec = by_day.get(d, {"calls": 0, "errors": 0})
        out.append({"day": d, "calls": rec["calls"], "errors": rec["errors"]})
    return out


_DB_NAME = {"sqlite": "SQLite", "mysql": "MySQL",
             "postgresql": "PostgreSQL", "mssql": "SQL Server"}


async def get_overview() -> dict:
    """概览看板聚合数据：单一事实源，所有计数来自 metadata 库。

    - 数据源 / 接口：从定义表实时 count
    - 今日调用/错误：从 api_logs 统计（与日志列表一致）
    - 14 天趋势：DailyStat 按天聚合（今日点用 api_logs 回填，避免重启/采样偏差）
    - 状态码分布 / 热门接口 / 最新调用：今日 api_logs 轻量聚合
    """
    today = date.today()
    today_start = datetime.combine(today, time.min)
    yesterday = (today - timedelta(days=1)).isoformat()

    async with SessionLocal() as s:
        # ---- 数据源 ----
        ds_rows = (await s.execute(
            select(DataSource.id, DataSource.name, DataSource.type, DataSource.status)
        )).all()
        ds_total = len(ds_rows)
        ds_ok = sum(1 for r in ds_rows if r.status == "ok")

        # ---- 接口 ----
        api_rows = (await s.execute(select(
            ApiDefinition.id, ApiDefinition.name, ApiDefinition.base_path,
            ApiDefinition.source_id, ApiDefinition.published, ApiDefinition.kind
        ))).all()
        api_total = len(api_rows)
        published = sum(1 for r in api_rows if r.published)
        custom_n = sum(1 for r in api_rows if r.kind == "custom")
        unpublished = api_total - published
        apis_per_ds = {}
        for r in api_rows:
            apis_per_ds[r.source_id] = apis_per_ds.get(r.source_id, 0) + 1

        # 路径 -> 接口名 映射（用于热门/最新调用展示接口名）
        src_name = {r.id: r.name for r in ds_rows}
        path_to_name = {}
        for r in api_rows:
            sn = src_name.get(r.source_id, "")
            p = f"/api/v1/{sn}/{r.base_path}".replace("//", "/")
            path_to_name[p] = r.name
        # 接口 id -> 名称：孤儿调用（数据源已删）也能按 api_id 反查名称，避免显示空白
        id_to_name = {r.id: r.name for r in api_rows}

        # ---- 今日调用 / 错误 / 延迟 ----
        calls_today, errors_today = await _today_log_counts()
        agg = (await s.execute(
            select(func.avg(ApiLog.latency_ms), func.max(ApiLog.latency_ms))
            .select_from(ApiLog).where(ApiLog.log_date >= today_start)
        )).first()
        avg_latency = int(round(agg[0] or 0))
        max_latency = int(agg[1] or 0)
        error_rate = round(errors_today / calls_today * 100, 2) if calls_today else 0.0

        # ---- 今日状态码分布 ----
        st_rows = (await s.execute(
            select(ApiLog.status_code, func.count())
            .select_from(ApiLog).where(ApiLog.log_date >= today_start)
            .group_by(ApiLog.status_code)
        )).all()
        dist = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
        for code, cnt in st_rows:
            if not code:
                continue
            if 200 <= code < 300:
                dist["2xx"] += int(cnt)
            elif 300 <= code < 400:
                dist["3xx"] += int(cnt)
            elif 400 <= code < 500:
                dist["4xx"] += int(cnt)
            elif code >= 500:
                dist["5xx"] += int(cnt)

        # ---- 热门接口 TOP（今日按 方法+路径 聚合）----
        top_rows = (await s.execute(
            select(ApiLog.method, ApiLog.path, ApiLog.api_id, func.count())
            .select_from(ApiLog).where(ApiLog.log_date >= today_start)
            .group_by(ApiLog.method, ApiLog.path, ApiLog.api_id)
            .order_by(func.count().desc()).limit(6)
        )).all()
        top_apis = [{"method": r[0], "path": r[1],
                     "name": path_to_name.get(r[1], "") or id_to_name.get(r[2] or "", ""),
                     "calls": int(r[3])}
                    for r in top_rows]

        # ---- 最新调用 ----
        rec = (await s.execute(
            select(ApiLog).where(ApiLog.log_date >= today_start)
            .order_by(ApiLog.log_date.desc()).limit(6)
        )).scalars().all()
        recent = [{"method": r.method, "path": r.path,
                   "name": path_to_name.get(r.path, "") or id_to_name.get(r.api_id or "", ""),
                   "status_code": r.status_code, "latency_ms": r.latency_ms,
                   # 与 admin_routes._iso_utc 保持一致的 UTC 序列化（带 Z），前端转本地时区
                   "created_at": r.log_date.strftime("%Y-%m-%dT%H:%M:%SZ") if r.log_date else ""} for r in rec]

    # ---- 7 天趋势（统一从 api_logs 按天聚合，单一事实源，重启/采样不影响）----
    days = 7
    start = today - timedelta(days=days - 1)
    trend = await _trend_from_logs(None, days, start)
    by_day = {t["day"]: t for t in trend}
    calls_yesterday = by_day.get(yesterday, {}).get("calls", 0)
    calls_delta_pct = (round((calls_today - calls_yesterday) / calls_yesterday * 100, 1)
                       if calls_yesterday else None)

    # ---- 系统信息 ----
    cfg = _get_cfg()
    db_dialect = getattr(engine.dialect, "name", "") if engine else ""
    uptime_s = int(_time.time() - _START)
    system = {
        "db": _DB_NAME.get(db_dialect, db_dialect or "unknown"),
        "uptime_seconds": uptime_s,
        "log_retention_days": cfg.get("log_retention_days"),
        "cors_enabled": bool(cfg.get("cors_enabled")),
        "slow_ms": cfg.get("slow_ms"),
        "sample_qps_threshold": cfg.get("sample_qps_threshold"),
    }

    return {
        "datasources": ds_total, "datasources_ok": ds_ok,
        "datasources_detail": [
            {"id": r.id, "name": r.name, "type": r.type, "status": r.status,
             "apis": apis_per_ds.get(r.id, 0)} for r in ds_rows],
        "apis": api_total, "published": published, "unpublished": unpublished,
        "custom": custom_n,
        "calls_today": calls_today, "errors_today": errors_today,
        "error_rate": error_rate, "avg_latency": avg_latency, "max_latency": max_latency,
        "calls_yesterday": calls_yesterday, "calls_delta_pct": calls_delta_pct,
        "status_dist": dist, "top_apis": top_apis, "recent": recent,
        "trend": trend, "system": system,
    }


async def get_trend(api_id: str, days: int = 30) -> list[dict]:
    start = date.today() - timedelta(days=days - 1)
    # 统一从 api_logs 按天聚合，与日志列表一致（不再依赖 DailyStat 双源）
    return await _trend_from_logs(api_id, days, start)


def _percentile(values: list, pct: int) -> int:
    """最近窗口延迟分位数（线性插值）。空列表返回 0。"""
    if not values:
        return 0
    v = sorted(values)
    n = len(v)
    if n == 1:
        return int(v[0])
    # 与 numpy 的线性插值对齐：idx = p*(n-1)
    k = (pct / 100) * (n - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return int(round(v[lo]))
    frac = k - lo
    return int(round(v[lo] + (v[hi] - v[lo]) * frac))


async def get_realtime(minutes: int = 60) -> dict:
    """实时指标：最近 minutes 分钟（封顶 1440）的逐分钟序列 + 窗口 KPI。

    单一事实源为 api_logs；方言相关的「按分钟分桶」由各库函数完成：
    - SQLite   strftime('%Y-%m-%d %H:%M', log_date)
    - MySQL    DATE_FORMAT(log_date, '%Y-%m-%d %H:%i')
    - PostgreSQL to_char(log_date, 'YYYY-MM-DD HH24:MI')
    各方言产出的桶 key 都是 'YYYY-MM-DD HH:MM' 字符串，与本地生成的时间轴对齐。

    返回：
    - series: 每分钟一条 {minute, calls, errors, avg_latency, cache_hits}
    - kpis:   {calls, errors, error_rate, avg_latency, p95_latency, p99_latency,
               cache_hits, cache_hit_rate, window_minutes, generated_at}
    """
    minutes = max(1, min(int(minutes), 1440))
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # 与库内 naive UTC 一致
    # 时间轴右端对齐「当前整分钟」，向左取 minutes 个整分钟桶，保证当前分钟永远是
    # 曲线最右点；SQL 过滤下界 cutoff 与轴起点精确对齐（同一整分钟），
    # 这样「通过过滤的行」与「轴上桶」一一对应，total = 轴上之和，绝不丢数也不重复。
    end_min = now.replace(second=0, microsecond=0)
    start_min = end_min - timedelta(minutes=minutes - 1)
    cutoff = start_min

    db_name = getattr(getattr(engine, "dialect", None), "name", "") or ""
    if db_name == "mysql":
        bucket = func.date_format(ApiLog.log_date, "%Y-%m-%d %H:%i")
    elif db_name == "postgresql":
        bucket = func.to_char(ApiLog.log_date, "YYYY-MM-DD HH24:MI")
    else:  # sqlite（默认）及未识别方言
        bucket = func.strftime("%Y-%m-%d %H:%M", ApiLog.log_date)

    async with SessionLocal() as s:
        rows = (await s.execute(
            select(
                bucket,
                func.count(),
                func.coalesce(func.sum(case((ApiLog.status_code >= 400, 1), else_=0)), 0),
                func.avg(ApiLog.latency_ms),
                func.coalesce(func.sum(case((ApiLog.cache_hit.is_(True), 1), else_=0)), 0),
            )
            .select_from(ApiLog)
            .where(ApiLog.log_date >= cutoff)
            .group_by(bucket)
        )).all()

        # 分位延迟需要逐行 latency；窗口通常不大（分钟级），可接受
        lat_rows = (await s.execute(
            select(ApiLog.latency_ms)
            .select_from(ApiLog)
            .where(ApiLog.log_date >= cutoff)
        )).scalars().all()

    by_bucket = {}
    for r in rows:
        by_bucket[str(r[0])] = {
            "calls": int(r[1] or 0),
            "errors": int(r[2] or 0),
            "avg_latency": int(round(r[3] or 0)),
            "cache_hits": int(r[4] or 0),
        }

    # 生成完整时间轴（对齐到整分钟），缺失分钟补 0，保证前端曲线连续
    series = []
    total_calls = total_errors = total_cache = 0
    start_min = cutoff.replace(second=0, microsecond=0)
    for i in range(minutes):
        bk = (start_min + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
        rec = by_bucket.get(bk, {"calls": 0, "errors": 0, "avg_latency": 0, "cache_hits": 0})
        series.append({
            "minute": bk,
            "calls": rec["calls"],
            "errors": rec["errors"],
            "avg_latency": rec["avg_latency"],
            "cache_hits": rec["cache_hits"],
        })
        total_calls += rec["calls"]
        total_errors += rec["errors"]
        total_cache += rec["cache_hits"]

    lats = [int(x) for x in lat_rows if x is not None]
    avg_latency = int(round(sum(lats) / len(lats))) if lats else 0
    p95 = _percentile(lats, 95)
    p99 = _percentile(lats, 99)
    error_rate = round(total_errors / total_calls * 100, 2) if total_calls else 0.0
    cache_hit_rate = round(total_cache / total_calls * 100, 2) if total_calls else 0.0

    return {
        "series": series,
        "kpis": {
            "window_minutes": minutes,
            "calls": total_calls,
            "errors": total_errors,
            "error_rate": error_rate,
            "avg_latency": avg_latency,
            "p95_latency": p95,
            "p99_latency": p99,
            "cache_hits": total_cache,
            "cache_hit_rate": cache_hit_rate,
        },
        "generated_at": now.isoformat(),
    }


async def maintain_logs() -> None:
    """按留存天数清理旧日志：对所有数据库生效（MySQL 走分区 DROP，其余走 DELETE）。

    - MySQL：保留高效的「预建次日分区 + DROP 超留存分区」。
    - SQLite / PostgreSQL（及未分区的 MySQL）：用 DELETE 删除 log_date 早于留存截止日的记录。
    """
    retention = _get_cfg().get("log_retention_days", settings.LOG_RETENTION_DAYS)
    if retention <= 0:
        return  # 留存天数 <=0 视为不清理
    today = date.today()
    tomorrow = today + timedelta(days=1)
    # 「保留 N 天」= 保留最近 N 个自然日（含今天）。
    # cutoff = today - (N-1)：N=1→只留今天；N=3→留今天+前两日。
    cutoff = today - timedelta(days=max(0, retention - 1))
    cutoff_dt = datetime(cutoff.year, cutoff.month, cutoff.day)

    # 1) MySQL 分区维护（高效）
    if IS_MYSQL:
        async with engine.begin() as conn:
            # 预建次日
            pname = f"p{tomorrow.strftime('%Y%m%d')}"
            try:
                await conn.execute(text(
                    f"ALTER TABLE api_logs ADD PARTITION ("
                    f"PARTITION {pname} VALUES LESS THAN (TO_DAYS('{tomorrow.isoformat()}')))"
                ))
            except Exception:
                pass
            # 丢弃超留存分区
            res = await conn.execute(text(
                "SELECT PARTITION_NAME FROM information_schema.PARTITIONS "
                "WHERE TABLE_NAME='api_logs' AND PARTITION_NAME IS NOT NULL "
                "AND PARTITION_NAME <> 'p_init' AND PARTITION_NAME <> 'pmax'"
            ))
            for (p,) in res.fetchall():
                try:
                    pd = date.strptime(p[1:] if p.startswith("p") else p, "%Y%m%d")
                except Exception:
                    continue
                if pd < cutoff:
                    try:
                        await conn.execute(text(f"ALTER TABLE api_logs DROP PARTITION {p}"))
                    except Exception:
                        pass

    # 2) 通用 DELETE：覆盖 SQLite / PostgreSQL，并作为「未分区 MySQL」的兜底
    #    （分区已删除时此处影响 0 行；未分区时负责真正清理）
    async with SessionLocal() as s:
        await s.execute(delete(ApiLog).where(ApiLog.log_date < cutoff_dt))
        await s.commit()
