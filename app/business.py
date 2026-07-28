"""业务面动态路由执行（对外，无鉴权）。

流程：查找已发布接口 → 方法校验 → 限流 → 熔断 →
执行（自定义 SQL）→ 计数 + 异步日志 → 统一 JSON 响应。
自定义 SQL 在适配层已做单语句/DDL/行数封顶校验（避坑 #29）。
"""
import re
import time
import json
import math
import hashlib
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import settings
from .cache import cache
from .publisher import get_entry
from .ratelimit import allow
from .circuit_breaker import is_open, record_success, record_failure
from .log_writer import record as log_record

SENSITIVE_KEYS = {"password", "token", "secret", "apikey", "api_key",
                  "authorization", "pwd", "passwd"}
_RESERVED = {"page", "size"}

# GET 只读强制：首关键字须为查询类，且整条语句不得出现写关键字
# 注意：SELECT ... INTO OUTFILE / SELECT LOAD_FILE() / SELECT ... FOR UPDATE /
#       SELECT ... LOCK IN SHARE MODE 等虽以 SELECT 开头但有破坏性或副作用，
#       须在写关键字表里一并拦截（H-5）。
# 注意：FOR / LOCK 不再作为整词 token 拦截（H-x）。列名/表名为 lock、for 的
# 查询会被误判为写操作而拒绝。其真正的写意图（FOR UPDATE / FOR SHARE /
# LOCK IN SHARE MODE / LOCK TABLES）已由下方 _DML_PHRASES 精确短语检查覆盖。
_WRITE_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE",
                   "UPSERT", "CALL", "EXEC", "EXECUTE",
                   "OUTFILE", "DUMPFILE", "LOAD_FILE",
                   "TRUNCATE", "ALTER", "DROP", "CREATE", "RENAME", "GRANT", "REVOKE"}
_READ_FIRST = {"SELECT", "WITH", "SHOW", "EXPLAIN", "DESC", "DESCRIBE", "VALUES"}

# FOR / LOCK 仅当其构成写意图时才拦截：FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE / LOCK TABLES
_DML_PHRASES = (("FOR", "UPDATE"), ("FOR", "SHARE"), ("LOCK", "IN"), ("LOCK", "TABLES"))


def _is_readonly_sql(sql: str) -> bool:
    """判断 SQL 是否为只读查询（用于 GET 强制只读）。

    先剔除字符串字面量/注释，再按「首关键字属于查询类」且「不含写关键字」判定。
    标识符含下划线视作整体 token（如 delete_log → DELETE_LOG ≠ DELETE），避免误杀。
    额外拦截 SELECT ... FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE / LOCK TABLES /
    INTO OUTFILE / LOAD_FILE 等具备破坏性或副作用的写法（H-5）。"""
    from .sources.sql import _strip_string_literals
    safe = _strip_string_literals((sql or "").strip().rstrip(";").strip())
    tokens = re.findall(r"[A-Za-z_]+", safe.upper())
    if not tokens:
        return True
    if tokens[0] not in _READ_FIRST:
        return False
    # 全 token 扫描写关键字（INSERT/UPDATE/.../OUTFILE/DUMPFILE/LOAD_FILE 等）
    if any(t in _WRITE_KEYWORDS for t in tokens):
        return False
    # 精确短语检查：FOR UPDATE / FOR SHARE / LOCK IN ... / LOCK TABLES
    for phrase in _DML_PHRASES:
        for i in range(len(tokens) - len(phrase) + 1):
            if tuple(tokens[i:i + len(phrase)]) == phrase:
                return False
    return True


# 提取 SQL 中的命名绑定参数 :name（忽略 PostgreSQL 的 ::type 强制转换与字符串字面量内的内容）
_BIND_RE = re.compile(r"(?<![:\w]):([a-zA-Z_]\w*)")


def _bind_names(sql: str) -> list:
    """返回 SQL 模板里出现的命名绑定参数（去重、保序）。

    先剔除字符串字面量与 SQL 注释（-- 行注释 / /* */ 块注释），避免把
    字面量或注释里的 ':xxx' 误判为绑定参数（如注释 `-- 按 :status 过滤`）。"""
    if not sql:
        return []
    from .sources.sql import _strip_string_literals
    no = _strip_string_literals(sql)
    seen, out = set(), []
    for m in _BIND_RE.finditer(no):
        n = m.group(1)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _normalize_params(raw):
    """把「入参声明」统一成 [{name,type,required,in,desc}]。
    兼容旧版 {name: type} 字典（全部 optional, in=query）；
    也支持新版的列表写法 [{name,type,required,in,desc}]。"""
    if not raw:
        return []
    if isinstance(raw, dict):
        out = []
        for k, v in raw.items():
            if isinstance(v, dict):
                out.append({"name": k,
                            "type": str(v.get("type", "string")),
                            "required": bool(v.get("required", False)),
                            "in": str(v.get("in", "query")),
                            "desc": str(v.get("desc", ""))})
            else:
                out.append({"name": k, "type": str(v),
                            "required": False, "in": "query", "desc": ""})
        return out
    if isinstance(raw, list):
        out = []
        for it in raw:
            if not isinstance(it, dict):
                continue
            name = it.get("name")
            if not name:
                continue
            out.append({"name": name,
                        "type": str(it.get("type", "string")),
                        "required": bool(it.get("required", False)),
                        "in": str(it.get("in", "query")),
                        "desc": str(it.get("desc", ""))})
        return out
    return []


def _coerce(value, typ):
    """按声明类型对入参做基本转换；失败抛 ValueError。"""
    t = (typ or "string").lower()
    try:
        if t in ("int", "integer"):
            return int(value)
        if t in ("float", "number", "decimal"):
            return float(value)
        if t in ("bool", "boolean"):
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in ("1", "true", "yes", "y", "t"):
                return True
            if s in ("0", "false", "no", "n", "f"):
                return False
            raise ValueError("非布尔值")
        return str(value)
    except (ValueError, TypeError):
        raise ValueError(f"无法把值 {value!r} 转为 {typ}")


def _redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if str(k).lower() in SENSITIVE_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def _caller_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real
    return request.client.host if request.client else ""


def _ok(data):
    return {"code": 0, "data": data, "message": "ok"}


def _err(code, message):
    return {"code": code, "message": message}


def _cache_key(api_id: str, sql: str, params: dict, page=None, size=None) -> str:
    """A1：只读响应缓存键 = api_id + SQL + 实际绑定参数（排序后）+ 分页。

    保证相同查询（含参数、含分页）命中同一缓存；不同参数/不同页/不同接口互不串扰。"""
    norm = json.dumps(params, sort_keys=True, default=str)
    pg = "" if page is None else f"|p={page}"
    sz = "" if size is None else f"|s={size}"
    raw = f"{api_id}|{sql}|{norm}{pg}{sz}"
    return "resp:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def execute(source: str, resource: str, rid, request: Request):
    start = time.time()
    entry = get_entry(source, resource)
    if not entry:
        # 即使接口未发布/未命中，也按路径反查归属接口，使 404 也能按接口筛选
        api_id = await _resolve_api_id(source, resource)
        await _log_miss(source, resource, request, 404, start, "资源不存在或未发布", api_id)
        return JSONResponse(_err(404, "资源不存在或未发布"), status_code=404)
    method = request.method.upper()
    if method not in entry.methods:
        await _count_and_log(entry, method, rid, request, "", 405, start, None, "", "方法不允许")
        return JSONResponse(_err(405, "方法不允许"), status_code=405)
    api_id, source_id = entry.api_id, entry.source_id
    ip = _caller_ip(request)

    # 限流（跨进程一致）
    # Redis 不可用时降级为「放行」（fail-open），避免把全部正常请求 429；仅告警。
    try:
        blocked = not await allow(ip, api_id)
    except Exception:
        logging.warning("限流缓存不可用，降级放行：%s/%s", source, resource)
        blocked = False
    if blocked:
        await _count_and_log(entry, method, rid, request, ip, 429, start, None, "", "")
        return JSONResponse(_err(429, "请求过于频繁"), status_code=429)

    # 熔断
    # Redis 不可用时降级为「闭合」（fail-open），避免误 503 全部请求；仅告警。
    try:
        open_ = await is_open(source_id)
    except Exception:
        logging.warning("熔断缓存不可用，降级为闭合：%s", source_id)
        open_ = False
    if open_:
        await _count_and_log(entry, method, rid, request, ip, 503, start, None, "", "")
        return JSONResponse(_err(503, "数据源暂时不可用"), status_code=503)

    params = {}
    if method == "GET":
        params = dict(request.query_params)
    else:
        raw = await request.body()
        if raw and raw.strip():
            try:
                parsed = json.loads(raw)
            except Exception as e:
                msg = f"请求体不是合法 JSON（常见原因：缺少逗号、多余逗号、引号未闭合）：{e}"
                await _count_and_log(entry, method, rid, request, ip, 400, start,
                                     None, "BadRequest", msg)
                return JSONResponse(_err(400, msg), status_code=400)
            if isinstance(parsed, dict):
                params = parsed
            elif parsed is None:
                params = {}
            else:
                msg = "请求体必须是 JSON 对象（形如 {\"key\": \"value\"}）"
                await _count_and_log(entry, method, rid, request, ip, 400, start,
                                     None, "BadRequest", msg)
                return JSONResponse(_err(400, msg), status_code=400)

    request.state.biz_params = params  # 供 _count_and_log 记录真实请求参数（不再误记响应体）

    # 自定义 SQL：预检命名绑定参数，缺参时给出清晰 400（避免底层晦涩的 500）
    needed = _bind_names(entry.sql)
    missing = [n for n in needed if n not in params]
    if missing:
        msg = ("缺少必填参数：" + ", ".join(missing) +
               "；请在请求体 JSON 中提供这些字段（注意大小写与 SQL 中的 :参数名 一致）")
        await _count_and_log(entry, method, rid, request, ip, 400, start,
                             None, "BadRequest", msg)
        return JSONResponse(_err(400, msg), status_code=400)

    # 「入参声明」生效：校验必填项，并按声明类型做基本转换
    for d in _normalize_params(entry.params):
        name = d["name"]
        if d["required"] and name not in params:
            msg = f"缺少必填入参：{name}（声明类型 {d['type']}）"
            await _count_and_log(entry, method, rid, request, ip, 400, start,
                                 None, "BadRequest", msg)
            return JSONResponse(_err(400, msg), status_code=400)
        if name in params and d["type"]:
            try:
                params[name] = _coerce(params[name], d["type"])
            except ValueError as e:
                msg = f"入参 {name} 类型错误：{e}"
                await _count_and_log(entry, method, rid, request, ip, 400, start,
                                     None, "BadRequest", msg)
                return JSONResponse(_err(400, msg), status_code=400)

    # GET 强制只读：GET 只允许查询语句（SELECT 等），杜绝把写操作暴露在 GET 上
    if method == "GET" and not _is_readonly_sql(entry.sql):
        msg = ("GET 请求只允许查询语句（SELECT）；该接口的 SQL 为写操作，"
               "请改用 POST/PUT/DELETE 方法调用。")
        await _count_and_log(entry, method, rid, request, ip, 400, start,
                             None, "ReadOnlyViolation", msg)
        return JSONResponse(_err(400, msg), status_code=400)

    # 只把 SQL 真正引用的命名绑定参数传给适配层，避免多余的 :name 触发
    # SQLAlchemy「Bind parameter without a render」500（数据库无该占位符）
    bound = set(_bind_names(entry.sql))
    exec_params = {k: v for k, v in params.items() if k in bound}

    # ---- A2：分页参数解析（仅 GET）----
    # 仅当请求显式带 page 且 SQL 未把 page 当作真实绑定参数时才启用分页，
    # 避免与业务里 :page 参数冲突；size/page_size 同理。page/size 不进 exec_params
    # （非 SQL 绑定参数），由适配层按分页语义消费。
    page_arg = size_arg = None
    if method == "GET" and "page" in params and "page" not in bound:
        try:
            page_arg = int(params["page"])
        except (TypeError, ValueError):
            page_arg = 1
        sz = params.get("size") if ("size" in params and "size" not in bound) else params.get("page_size")
        if sz is not None:
            try:
                size_arg = int(sz)
            except (TypeError, ValueError):
                size_arg = None

    # ---- A1：只读响应缓存（仅 GET，已强制只读；写请求不缓存）----
    # 按 (api_id + SQL + 实际绑定参数 + 分页) 计算缓存键，重复查询直接命中，跳过数据库执行。
    # 每个接口可在 overrides.cache 里关闭缓存或单独设置 TTL（秒）。
    use_cache = (method == "GET" and settings.API_CACHE_ENABLED)
    cache_key = None
    cache_ttl = settings.API_CACHE_TTL
    if use_cache:
        ov = entry.overrides or {}
        if isinstance(ov, dict) and isinstance(ov.get("cache"), dict):
            cc = ov["cache"]
            if cc.get("enabled") is False:
                use_cache = False
            elif cc.get("ttl"):
                try:
                    cache_ttl = int(cc["ttl"])
                except (TypeError, ValueError):
                    pass
        if use_cache:
            cache_key = _cache_key(entry.api_id, entry.sql, exec_params, page_arg, size_arg)
            try:
                cached = await cache.cache_get(cache_key)
            except Exception:
                logging.warning("响应缓存读取失败，降级为回源：%s", cache_key)
                cached = None
            if cached:
                try:
                    hit = json.loads(cached)
                    await _count_and_log(entry, method, rid, request, ip, 200, start,
                                         hit.get("data"), "", "cache-hit",
                                         cache_hit=True)
                    return JSONResponse(hit)
                except Exception:
                    pass  # 缓存损坏，继续走数据库

    try:
        data = await entry.adapter.exec_sql(entry.sql, exec_params, method,
                                            page=page_arg, size=size_arg)
        await record_success(source_id)
        status = 200
        err_type = err_detail = ""
        # A1：执行成功后写回缓存（仅 GET 命中缓存场景）
        if use_cache and cache_key:
            try:
                payload = _ok(_sanitize(data))
                await cache.cache_set(cache_key, json.dumps(payload), cache_ttl)
            except Exception:
                logging.exception("响应缓存写入失败（不影响主流程）")
    except Exception as e:
        await record_failure(source_id)
        status = 500
        data = None
        err_type = type(e).__name__
        err_detail = str(e)
        # 原始异常仅入内部日志，不返回给客户端（避免泄露表名/SQL/连接串，H-2）
        logging.error("SQL 执行失败 [%s]: %s", err_type, err_detail)
        await _count_and_log(entry, method, rid, request, ip, status, start,
                             None, err_type, err_detail)
        return JSONResponse(_err(500, "内部执行错误，详情请查看服务端日志"), status_code=500)

    await _count_and_log(entry, method, rid, request, ip, status, start,
                         data, "", "")
    return JSONResponse(_ok(_sanitize(data)))


async def _count_and_log(entry, method, rid, request, ip, status, start, data, err_type, err_detail,
                      params=None, cache_hit: bool = False):
    latency = int((time.time() - start) * 1000)
    day = date.today().isoformat()
    try:
        await cache.incr_counter(f"stats:calls:{entry.api_id}:{day}")
        if status >= 400:
            await cache.incr_counter(f"stats:errors:{entry.api_id}:{day}")
    except Exception:
        logging.warning("统计计数写入失败（不影响主流程）：%s", entry.api_id)

    # 记录「请求参数」而非响应体：优先用显式传入的 params，否则取 request.state.biz_params
    # （execute 在解析完入参后写入）。旧逻辑对非 GET 把响应体写进了 query_params 列，
    # 会膨胀日志、甚至把返回数据写进日志库。
    if params is None:
        params = getattr(request.state, "biz_params", None)
    if params is None:
        params_log = _redact(dict(request.query_params)) if method == "GET" else {}
    else:
        params_log = _redact(dict(params))
    rec = {
        # 显式写入 UTC（naive UTC），避免依赖 DB 的 func.now()：SQLite 的 CURRENT_TIMESTAMP
        # 返回 UTC，而 MySQL 的 NOW() 返回服务器时区，二者不一致会导致展示时间错位。
        # 统一存 UTC（与 stats.get_realtime 的 UTC 假设一致），前端再按浏览器本地时区展示。
        "log_date": datetime.now(timezone.utc).replace(tzinfo=None),
        "api_id": entry.api_id,
        "source_id": entry.source_id,
        "method": method,
        "path": str(request.url.path),
        "query_params": params_log,
        "status_code": status,
        "latency_ms": latency,
        "request_size": int(request.headers.get("content-length", 0) or 0),
        "response_size": len(json.dumps(_sanitize(data), default=str)) if data is not None else 0,
        "caller_ip": ip,
        "user_agent": request.headers.get("user-agent", ""),
        "error_type": err_type or "",
        "error_detail": (_redact({"e": err_detail})["e"] if err_detail else ""),
        "cache_hit": bool(cache_hit),
    }
    try:
        await log_record(rec)
    except Exception:
        logging.warning("调用日志写入失败（不影响主流程）")


async def _resolve_api_id(source: str, resource: str) -> str:
    """miss/404 时按 (数据源名 + 路径段) 反查接口定义（含未发布），
    把调用日志归属到对应接口，使按接口筛选 404 也能命中。"""
    if not source:
        return ""
    from sqlalchemy import func, select
    from .metadata_db import SessionLocal
    from .models import ApiDefinition, DataSource
    try:
        async with SessionLocal() as s:
            ds = (await s.execute(
                select(DataSource).where(func.lower(DataSource.name) == source.lower())
            )).scalars().first()
            if not ds:
                return ""
            rows = (await s.execute(
                select(ApiDefinition).where(ApiDefinition.source_id == ds.id)
            )).scalars().all()
            rest = (resource or "").lower().strip("/")
            best, best_len = "", -1
            for a in rows:
                rk = (a.base_path) or ""
                rk = rk.lower().strip("/")
                if not rk:
                    continue
                if rest == rk or rest.startswith(rk + "/"):
                    if len(rk) > best_len:
                        best_len, best = len(rk), a.id
            return best
    except Exception:
        return ""


async def _log_miss(source: str, resource: str, request: Request, status: int,
                    start: float, message: str, api_id: str = ""):
    """路径未命中（无 entry）时也写一条调用日志，保证非 200 都被记录。
    若已按路径反查出归属接口(api_id)，则一并写入，便于按接口筛选。"""
    latency = int((time.time() - start) * 1000)
    day = date.today().isoformat()
    ip = _caller_ip(request)
    # 未命中计数归入通用桶（api_id 为空），归属接口与否都记录
    try:
        await cache.incr_counter(f"stats:calls::{day}")
        if status >= 400:
            await cache.incr_counter(f"stats:errors::{day}")
    except Exception:
        logging.warning("统计计数写入失败（不影响主流程）")
    params_log = _redact(dict(request.query_params))
    rec = {
        "log_date": datetime.now(timezone.utc).replace(tzinfo=None),
        "api_id": api_id or "",
        "source_id": source or "",
        "method": request.method.upper(),
        "path": str(request.url.path),
        "query_params": params_log,
        "status_code": status,
        "latency_ms": latency,
        "request_size": int(request.headers.get("content-length", 0) or 0),
        "response_size": 0,
        "caller_ip": ip,
        "user_agent": request.headers.get("user-agent", ""),
        "error_type": "",
        "error_detail": _redact({"e": message})["e"],
        "cache_hit": False,
    }
    try:
        await log_record(rec)
    except Exception:
        logging.warning("调用日志写入失败（不影响主流程）")
