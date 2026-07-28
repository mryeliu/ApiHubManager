"""关系型数据源适配（MySQL / PostgreSQL / SQL Server）。

用 SQLAlchemy Core 反射 + 参数化执行：
- 表名/列名来自反射白名单，杜绝字符串拼接注入（避坑 #3）
- 方言差异（分页/标识符引用）由 SQLAlchemy 自动处理（避坑 #14）
- 读主写从按配置路由（避坑 #12）
- 引擎按 source_id 进程内缓存，连接池大小可配（避坑 #13）
- 所有执行均经 AsyncEngine.connect()/begin()（SQLAlchemy 2.0 异步规范）
"""
import re
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import quote_plus

from sqlalchemy import (
    MetaData,
    Table,
    text,
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine

from ..config import settings
from ..crypto import decrypt_secret
from .base import SourceAdapter, register

# source_id -> (write_engine, read_engine|None)
_ENGINES: dict[str, tuple[AsyncEngine, Optional[AsyncEngine]]] = {}

_DDL_KEYWORDS = ("CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE", "RENAME")


# 匹配 SQL 末尾的 LIMIT / OFFSET 子句里出现的命名绑定参数（:name）。
# MySQL 的 LIMIT/OFFSET 不接受字符串参数（LIMIT '1', 10 会语法错误），
# 必须把这类参数的值强制转成 int 再传给驱动。
_LIMIT_OFFSET_BIND_RE = re.compile(
    r"\b(?:LIMIT|OFFSET)\b\s+(?::([a-zA-Z_]\w*)\s*,?\s*)*"
    r"(?::([a-zA-Z_]\w*))?",
    re.IGNORECASE,
)


def _limit_offset_bind_names(sql: str) -> set:
    """返回出现在 LIMIT/OFFSET 子句里的命名绑定参数名集合。

    实现：从剥离字符串字面量/注释后的 SQL 里逐个找 LIMIT / OFFSET 关键字，
    再扫描其后到下一个 SQL 关键字（或行尾）之间的 :name 占位符。
    """
    if not sql:
        return set()
    safe = _strip_string_literals(sql)
    names: set = set()
    # 逐个定位 LIMIT / OFFSET 关键字位置
    for kw in ("LIMIT", "OFFSET"):
        pattern = re.compile(r"\b" + kw + r"\b", re.IGNORECASE)
        for m in pattern.finditer(safe):
            # 取关键字之后的一段文本（到下一个常见 SQL 子句关键字或分号/结尾）
            tail = safe[m.end():]
            # 截断到下一个可能结束 LIMIT 区段的位置
            end = len(tail)
            for stop_kw in (";", "FROM", "WHERE", "ORDER", "GROUP", "HAVING",
                            "UNION", "INTERSECT", "EXCEPT", "RETURNING"):
                idx = re.search(r"\b" + stop_kw + r"\b", tail, re.IGNORECASE)
                if idx:
                    end = min(end, idx.start())
            segment = tail[:end]
            for bm in re.finditer(r"(?<![:\w]):([a-zA-Z_]\w*)", segment):
                names.add(bm.group(1))
    return names


def _coerce_limit_offset_params(sql: str, params: dict) -> dict:
    """把出现在 LIMIT/OFFSET 里的绑定参数值强制转为 int。

    MySQL 的 LIMIT 不接受字符串参数（'1' 会触发 1064 语法错误）；
    即使用户在「入参声明」里漏标类型，这里也兜底转成 int，避免 500。
    转换失败（非数字）则原样返回，让数据库报清晰错误。
    """
    if not params:
        return params
    targets = _limit_offset_bind_names(sql)
    if not targets:
        return params
    out = dict(params)
    for name in targets:
        if name in out and not isinstance(out[name], bool):
            v = out[name]
            if isinstance(v, int):
                continue
            try:
                out[name] = int(v)
            except (ValueError, TypeError):
                # 非数字值，保持原样，由数据库给出明确错误
                pass
    return out


def _strip_string_literals(sql: str) -> str:
    """去除单/双引号字符串字面量与注释内容，使其中出现的 ';' 等不会被误判为多语句。

    处理转义引号（'' 与 "" 风格）、行注释（--）与块注释（/* */）。仅用于校验，
    不改变实际执行的 SQL。
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        # 行注释
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        # 块注释
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        # 单引号字符串字面量
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    # 两个连续单引号视为转义（''），跳过第二个
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        # 双引号字符串（部分方言作标识符，统一去除内容）
        if c == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# 只读判定（供 query-preview 使用）：首关键字须为查询类，避免管理员误执行写操作。
_READ_FIRST = {"SELECT", "WITH", "SHOW", "EXPLAIN", "DESCRIBE", "VALUES"}


def _is_readonly(sql: str) -> bool:
    safe = _strip_string_literals((sql or "").strip().rstrip(";").strip())
    tokens = re.findall(r"[A-Za-z_]+", safe.upper())
    return bool(tokens) and tokens[0] in _READ_FIRST


def _strip_named_params(sql: str) -> str:
    """把 :name 命名绑定占位替换成 NULL（仅 EXPLAIN 降级展示用，避免缺参时报错）。"""
    return re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "NULL", sql or "")


def _safe_default(default) -> Optional[str]:
    """把列默认值的各种形态（None / FetchedValue / 文本）规整为可序列化字符串。"""
    if default is None:
        return None
    # FetchedValue / 其它非标量对象：不展示内部表示，返回 None
    if not isinstance(default, (str, int, float, bool)):
        return None
    return str(default)


# A5：预编译缓存——把解析后的 text() 对象按 SQL 文本缓存，避免每次重解析。
# 高并发下减少解析开销；text 对象不可变、可跨执行复用。
_COMPILED: dict = {}
_COMPILED_MAX = 1024


def _compiled(sql: str):
    c = _COMPILED.get(sql)
    if c is None:
        c = text(sql)
        if len(_COMPILED) >= _COMPILED_MAX:
            _COMPILED.clear()
        _COMPILED[sql] = c
    return c


_PAGINATION_RE = re.compile(r"(?<![:\w]):(pageSize|offset|pageIndex)\b")
_LIMIT_RE = re.compile(r"\bLIMIT\b", re.I)
_TOP_RE = re.compile(r"\bTOP\s*\([^)]*\)", re.I)


def _has_explicit_pagination(sql: str, tp: str) -> bool:
    """SQL 是否已自带分页（生成器的 :pageSize/:offset/:pageIndex，或用户手写的 LIMIT/TOP）。"""
    if _PAGINATION_RE.search(sql or ""):
        return True
    if tp == "sqlserver":
        return bool(_TOP_RE.search(sql or ""))
    return bool(_LIMIT_RE.search(sql or ""))


def _strip_pagination(sql: str, tp: str) -> str:
    """去掉末尾 LIMIT..OFFSET..（或 SQL Server 的 TOP(..) 与 ORDER BY），用于 COUNT(*) 统计总行数。

    - MySQL/PG/SQLite：去掉末尾 `LIMIT :pageSize [OFFSET :offset]` 或字面 `LIMIT n [OFFSET m]`；
    - SQL Server：去掉 `TOP (...)`，并把派生表不允许的尾部 `ORDER BY ...` 一并去掉（否则 COUNT 子查询报语法错）。
    """
    s = (sql or "").strip().rstrip(";").strip()
    if tp == "sqlserver":
        s = _TOP_RE.sub("", s, count=1)
        s = re.sub(r"\s+ORDER\s+BY\s+.+$", "", s, flags=re.I | re.DOTALL)
    else:
        s = re.sub(r"\s+LIMIT\s+:pageSize(\s+OFFSET\s+:offset)?\s*$", "", s, flags=re.I)
        s = re.sub(r"\s+LIMIT\s+\d+(\s+OFFSET\s+\d+)?\s*$", "", s, flags=re.I)
    return s


def _paginate_sql(sql: str, limit: int, offset: int, tp: str) -> str:
    """A2：方言相关的分页包裹。

    用派生表包裹原 SQL 再施加 LIMIT/OFFSET。limit/offset 为内部整数（已校验），
    直接内联，既避免与用户绑定参数重名，也杜绝注入（值非用户输入）。
    """
    if tp == "sqlserver":
        return (f"SELECT * FROM ({sql}) AS _pg "
                f"ORDER BY (SELECT NULL) OFFSET {int(offset)} ROWS "
                f"FETCH NEXT {int(limit)} ROWS ONLY")
    # mysql / postgresql / sqlite 均支持 LIMIT ... OFFSET ...
    return f"SELECT * FROM ({sql}) AS _pg LIMIT {int(limit)} OFFSET {int(offset)}"


def _is_insert(sql: str) -> bool:
    """判断是否为 INSERT 语句（用于 PG lastrowid 兜底）。"""
    return re.match(r"\s*INSERT\s+INTO\b", sql, re.IGNORECASE) is not None


# 各类型默认端口：配置里 port 为空时用，避免 URL 出现 `host:/db` 这种畸形串
_DEFAULT_PORTS = {"mysql": "3306", "postgresql": "5432", "sqlserver": "1433"}
# SQL Server 走 aioodbc（底层 pyodbc），driver 名必须与本机已安装的 ODBC 驱动一致。
# 不同环境装的不同：Docker 镜像与 Rocky 原生部署装「ODBC Driver 18 for SQL Server」，
# Windows 开发机常见 17，老环境可能只有旧版「SQL Server」原生驱动。
# 为避免硬编码驱动名导致 IM002（Data source name not found / no default driver），
# 改为运行时自动探测本机已安装的驱动（见 _detect_mssql_driver）。
_DEFAULT_MSSQL_DRIVER = "ODBC Driver 18 for SQL Server"


def _list_odbc_drivers() -> list[str]:
    """返回本机已安装的 ODBC 驱动名列表（进程内每次调用实时查询，开销极低）。"""
    try:
        import pyodbc
        return [d for d in pyodbc.drivers() if d]
    except Exception:
        return []


def _detect_mssql_driver(cfg: dict) -> str:
    """为 SQL Server 数据源挑选本机确实存在的 ODBC 驱动，避免 IM002。

    优先级：
      1. 数据源显式配置的 odbc_driver（若该驱动本机存在）；
      2. 文档约定默认 _DEFAULT_MSSQL_DRIVER（若存在）；
      3. 已安装的『ODBC Driver NN for SQL Server』中版本号最大者；
      4. 旧版『SQL Server』原生驱动（兜底）；
    若本机完全没有 SQL Server 相关 ODBC 驱动，抛出可操作的清晰错误。
    """
    candidates: list[str] = []
    configured = cfg.get("odbc_driver")
    if configured:
        candidates.append(configured)
    candidates.append(_DEFAULT_MSSQL_DRIVER)

    drivers = _list_odbc_drivers()
    modern: list[tuple[int, str]] = []
    legacy = None
    for d in drivers:
        if "SQL Server" not in d:
            continue
        m = re.search(r"ODBC Driver\s+(\d+)\s+for SQL Server", d)
        if m:
            modern.append((int(m.group(1)), d))
        elif d.strip() == "SQL Server":
            legacy = d
    modern.sort(reverse=True)
    for _, d in modern:
        candidates.append(d)
    if legacy:
        candidates.append(legacy)

    for c in candidates:
        if c in drivers:
            if configured and c != configured:
                logging.warning(
                    "数据源配置的 ODBC 驱动 %r 在本机未安装，已自动改用 %r。",
                    configured, c)
            return c

    installed = ", ".join(drivers) or "（无）"
    raise RuntimeError(
        "本机未安装任何 SQL Server 的 ODBC 驱动，无法连接。请安装 Microsoft "
        "'ODBC Driver 17/18 for SQL Server' "
        "(https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)。"
        f"当前已安装驱动：{installed}")


def _enc(s) -> str:
    """URL 编码：密码/库名含 @ : / # 空格 等字符会破坏连接串，必须编码（避坑）。"""
    return quote_plus(s) if s else ""


def _build_url(tp: str, cfg: dict, read: bool = False) -> str:
    host = _enc(cfg.get("read_host" if read else "host"))
    port = _enc(str(cfg.get("read_port" if read else "port")
                    or _DEFAULT_PORTS.get(tp, "")))
    user = _enc(cfg.get("read_user" if read else "user"))
    raw_pw = cfg.get("read_password" if read else "password", "")
    pw = _enc(decrypt_secret(raw_pw)) if raw_pw else ""
    db = _enc(cfg.get("read_database" if read else "database"))
    if tp == "mysql":
        return f"mysql+aiomysql://{user}:{pw}@{host}:{port}/{db}"
    if tp == "postgresql":
        return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"
    if tp == "sqlserver":
        driver = quote_plus(_detect_mssql_driver(cfg))
        return (f"mssql+aioodbc://{user}:{pw}@{host}:{port}/{db}"
                f"?driver={driver}&TrustServerCertificate=yes")
    raise ValueError(f"不支持的类型: {tp}")


def _get_engines(source_id: str, tp: str, cfg: dict) -> tuple[AsyncEngine, Optional[AsyncEngine]]:
    if source_id in _ENGINES:
        return _ENGINES[source_id]
    # 连接池大小按配置注入（避坑 #13 延伸：生产高并发需显式放大，
    # 默认 pool_size=10 / max_overflow=20，不再依赖 SQLAlchemy 内置 5/10）。
    pool_kwargs = dict(
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
        future=True,
    )
    write = create_async_engine(_build_url(tp, cfg), **pool_kwargs)
    read = None
    ru = cfg.get("read_url")
    if ru:
        # A4：整串只读副本连接串（最简洁的读写分离配置方式）
        read = create_async_engine(ru, **pool_kwargs)
    elif cfg.get("read_host"):
        # A4：拆分式只读副本配置：缺少必要字段则降级到主库并记录告警
        # （避免半成品配置连不上导致只读请求整体失败）。
        missing = [k for k in ("read_password", "read_database") if not cfg.get(k)]
        if missing:
            logging.warning(
                "数据源 %s 配置了 read_host 但缺少 %s，只读副本降级为主库。",
                source_id, ",".join(missing))
        else:
            read = create_async_engine(_build_url(tp, cfg, read=True), **pool_kwargs)
    _ENGINES[source_id] = (write, read)
    return write, read


async def dispose_engines(source_id: str) -> None:
    """释放某数据源缓存的引擎连接池，避免删除/更新配置后泄漏（H-3）。

    临时测试引擎键以 `__test_` 开头，不在 _ENGINES 常驻，无需处理；
    但 delete_datasource 应调用本函数释放真实 source_id 的引擎。
    """
    item = _ENGINES.pop(source_id, None)
    if not item:
        return
    write, read = item
    for eng in (write, read):
        if eng is not None:
            try:
                await eng.dispose()
            except Exception:
                pass


async def _reflect_table(engine: AsyncEngine, table: str, schema: Optional[str]) -> Table:
    meta = MetaData()
    async with engine.connect() as conn:
        table_obj = await conn.run_sync(
            lambda c, t=table, s=schema: Table(t, meta, autoload_with=c, schema=s)
        )
    return table_obj


@asynccontextmanager
async def _conn(self, write: bool):
    """按读写选择引擎并打开连接（写自动提交用 begin）。"""
    w, r = self._engines()
    eng = w if write else (r or w)
    if write:
        async with eng.begin() as conn:
            yield conn
    else:
        async with eng.connect() as conn:
            yield conn


class RelationalAdapter(SourceAdapter):
    def __init__(self, source_id: str, source_type: str, config: dict):
        self.source_id = source_id
        self.source_type = source_type
        self.config = config

    def _engines(self):
        return _get_engines(self.source_id, self.source_type, self.config)

    async def test_connection(self) -> tuple[bool, str]:
        """返回 (是否成功, 错误信息)。错误信息用于前端排查，不再黑盒返回 False。"""
        try:
            write, _ = self._engines()
            async with write.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True, ""
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            # 连接层常见故障给出更直白的中文提示，便于前端/用户自查（自检增强）
            if "IM002" in msg or "Data source name not found" in msg \
                    or "no default driver" in msg:
                msg += ("（ODBC 驱动未找到：本机未安装或未正确配置 SQL Server 的 "
                        "ODBC 驱动，请在数据源配置 odbc_driver 或安装对应驱动）")
            elif "Login failed" in msg or "登录失败" in msg:
                msg += "（登录失败：请检查用户名/密码/数据库名是否正确）"
            elif "Could not connect" in msg or "timed out" in msg.lower() \
                    or "timeout" in msg.lower():
                msg += "（无法连接：请检查主机地址/端口/网络/防火墙是否可达）"
            return False, msg

    async def list_tables(self, q: str = "", schema: str = "",
                          page: int = 1, size: int = 50) -> dict:
        write, _ = self._engines()
        async with write.connect() as conn:
            names = await conn.run_sync(
                lambda c: __import__("sqlalchemy").inspect(c).get_table_names(schema=schema or None)
            )
        if q:
            ql = q.lower()
            names = [n for n in names if ql in n.lower()]
        total = len(names)
        start = (max(page, 1) - 1) * size
        paged = names[start:start + size]
        return {"items": paged, "total": total, "page": page, "size": size}

    async def get_table_meta(self, table: str) -> dict:
        write, _ = self._engines()
        tbl = await _reflect_table(write, table, self.config.get("schema"))
        cols = [{"name": c.name, "type": str(c.type)} for c in tbl.columns]
        pk = [c.name for c in tbl.primary_key.columns]
        return {"table": table, "columns": cols, "primary_key": pk}

    async def list_schemas(self) -> list:
        """列出数据源的 schema（MySQL=库；PG=模式；SQL Server=架构）。

        用于 Schema 浏览器切换命名空间；PG 过滤掉 information_schema 等系统模式。
        """
        write, _ = self._engines()
        async with write.connect() as conn:
            names = await conn.run_sync(
                lambda c: __import__("sqlalchemy").inspect(c).get_schema_names()
            )
        if self.source_type == "postgresql":
            skip = {"information_schema", "pg_catalog", "pg_toast",
                    "pg_temp_1", "pg_toast_temp_1"}
            names = [n for n in names if n not in skip]
        return sorted(names)

    async def list_columns(self, table: str, schema: str = "") -> dict:
        """反射单表字段：列名/类型/可空/默认值/是否主键（用于 Schema 浏览器点选拼 SQL）。"""
        if not table:
            raise ValueError("table 不能为空")
        write, _ = self._engines()
        async with write.connect() as conn:
            insp = await conn.run_sync(lambda c: __import__("sqlalchemy").inspect(c))
            cols = await conn.run_sync(
                lambda c: insp.get_columns(table, schema=schema or None)
            )
            pk = await conn.run_sync(
                lambda c: insp.get_pk_constraint(table, schema=schema or None)
            )
        pk_cols = (pk or {}).get("constrained_columns") or []
        out = []
        for c in cols:
            out.append({
                "name": c["name"],
                "type": str(c["type"]),
                "nullable": bool(c.get("nullable", True)),
                "default": _safe_default(c.get("default")),
                "primary_key": c["name"] in pk_cols,
            })
        return {"table": table, "schema": schema or "", "columns": out,
                "primary_key": pk_cols}

    async def preview_sql(self, sql_template: str, limit: int = 50,
                          params: dict = None, explain: bool = False) -> dict:
        """管理员查询预览：仅允许只读查询，硬上限封顶，绝不执行写操作。

        - _validate_sql 拦截多语句 / DDL；
        - _is_readonly 进一步要求首关键字为查询类（SELECT/WITH/...）；
        - 结果外层包裹 LIMIT 控制行数，避免大结果集拖垮管理后台。
        """
        sql_template = (sql_template or "").strip().rstrip(";").strip()
        self._validate_sql(sql_template)            # 多语句 / DDL 拦截
        if not _is_readonly(sql_template):          # 只读二次保障
            raise ValueError("预览仅支持只读查询（SELECT / WITH ... SELECT 等）")
        limit = max(1, min(int(limit or 50), settings.CUSTOM_SQL_MAX_ROWS))
        params = dict(params or {})
        tp = self.source_type
        # 前端 Schema 浏览器生成的 SELECT 使用 :pageSize / :offset 绑定分页；友好的 :pageIndex
        # 由调用方换算为 offset=(pageIndex-1)*pageSize。仅在 SQL 实际出现该占位时补/换算，
        # 避免方言无关地注入多余参数（SQLAlchemy 对多余命名参数会报错）。
        # 注：OFFSET 必须是纯参数，绝不能写算术表达式（MySQL 拒绝 LIMIT/OFFSET 内嵌表达式）。
        if _PAGINATION_RE.search(sql_template):
            if "pageSize" not in params:
                params["pageSize"] = limit
            else:
                params["pageSize"] = min(int(params.get("pageSize") or limit), limit)
            if tp != "sqlserver":
                if "pageIndex" in params and "offset" not in params:
                    params["offset"] = (int(params.get("pageIndex") or 1) - 1) * int(params["pageSize"])
                if "offset" not in params:
                    params["offset"] = 0
            params.pop("pageIndex", None)  # pageIndex 仅用于换算，不接入库
        # 仅当用户 SQL 完全没有分页（裸 SELECT）时才用派生表封顶，避免大结果集拖垮后台；
        # 生成器已带 LIMIT/TOP 的 SQL 直接执行，不再包裹（避免重复 LIMIT + OFFSET 表达式导致的 1064）。
        if _has_explicit_pagination(sql_template, tp):
            safe_sql = sql_template
        elif tp == "sqlserver":
            safe_sql = (f"SELECT * FROM ({sql_template}) AS _pg "
                        f"ORDER BY (SELECT NULL) OFFSET 0 ROWS FETCH NEXT {int(limit)} ROWS ONLY")
        else:
            safe_sql = f"SELECT * FROM ({sql_template}) AS _pg LIMIT {int(limit)} OFFSET 0"
        write, read = self._engines()
        eng = read or write
        out: dict = {}
        if explain:
            try:
                out["explain"] = await self.explain(safe_sql, params)
            except Exception as e:
                out["explain_error"] = f"EXPLAIN 不可用：{e}"
        async with eng.connect() as conn:
            try:
                # MySQL 的 LIMIT/OFFSET 不接受字符串参数，兜底转 int（与 exec_sql 一致）
                params = _coerce_limit_offset_params(safe_sql, params)
                result = await conn.execute(_compiled(safe_sql), params)
            except Exception as e:
                raise ValueError(f"参数绑定或执行失败：{e}")
            rows = result.mappings().all()
            cols = list(rows[0].keys()) if rows else list(result.keys())
        out.update({"columns": cols, "rows": [dict(r) for r in rows],
                    "limit": limit, "count": len(rows)})
        return out

    async def explain(self, sql_template: str, params: dict = None) -> dict:
        """方言感知的只读执行计划：SQLite 用 EXPLAIN QUERY PLAN，MySQL/PG 用 EXPLAIN，
        SQL Server 用 SET SHOWPLAN_ALL。仅对只读查询生效，返回已格式化的 plan 文本行。

        params 透传用于 EXPLAIN 参数化 SQL（preview_sql 的 explain=True 路径会传入）；
        若未传且 SQL 仍含 :name 绑定占位，则自动剥离占位后再 EXPLAIN（仅用于结构展示）。
        """
        sql_template = (sql_template or "").strip().rstrip(";").strip()
        self._validate_sql(sql_template)
        if not _is_readonly(sql_template):
            raise ValueError("EXPLAIN 仅支持只读查询（SELECT / WITH ... SELECT 等）")
        params = params or {}
        write, read = self._engines()
        eng = read or write
        tp = self.source_type
        async with eng.connect() as conn:
            if tp == "sqlite":
                stmt = f"EXPLAIN QUERY PLAN {sql_template}"
                try:
                    result = await conn.execute(_compiled(stmt), params)
                except Exception:
                    result = await conn.execute(
                        _compiled(_strip_named_params(stmt)))
                rows = result.mappings().all()
                plan = [f"{r.get('id')} | parent={r.get('parent')} | "
                        f"{r.get('notused')} | {r.get('detail')}" for r in rows]
                return {"dialect": tp, "plan": plan}
            if tp == "sqlserver":
                try:
                    await conn.execute(_compiled("SET SHOWPLAN_ALL ON"))
                    try:
                        result = await conn.execute(_compiled(sql_template), params)
                    except Exception:
                        result = await conn.execute(
                            _compiled(_strip_named_params(sql_template)))
                    cols = list(result.keys())
                    rows = result.mappings().all()
                    plan = [" | ".join(str(r.get(c, "")) for c in cols)
                            for r in rows]
                finally:
                    await conn.execute(_compiled("SET SHOWPLAN_ALL OFF"))
                return {"dialect": tp, "plan": plan}
            # mysql / postgresql
            stmt = f"EXPLAIN {sql_template}"
            try:
                result = await conn.execute(_compiled(stmt), params)
            except Exception:
                result = await conn.execute(_compiled(_strip_named_params(stmt)))
            cols = list(result.keys())
            rows = result.mappings().all()
            if cols == ["QUERY PLAN"]:  # postgresql
                plan = [str(r.get("QUERY PLAN", "")) for r in rows]
            else:  # mysql 表格形式
                plan = [" | ".join(f"{c}={r.get(c, '')}" for c in cols)
                        for r in rows]
            return {"dialect": tp, "plan": plan}

    async def list_foreign_keys(self, table: str = "", schema: str = "") -> dict:
        """反射单表外键（用于查询构建器的 JOIN 自动关联建议）。

        返回该表指向其它表的外键：本表列 / 引用表 / 引用列，前端据此自动生成 ON 条件。
        """
        if not table:
            raise ValueError("table 不能为空")
        write, _ = self._engines()
        async with write.connect() as conn:
            insp = await conn.run_sync(
                lambda c: __import__("sqlalchemy").inspect(c))
            fks = await conn.run_sync(
                lambda c: insp.get_foreign_keys(table, schema=schema or None))
        out = []
        for fk in (fks or []):
            out.append({
                "name": fk.get("name"),
                "columns": fk.get("constrained_columns") or [],
                "referred_table": fk.get("referred_table"),
                "referred_columns": fk.get("referred_columns") or [],
                "referred_schema": fk.get("referred_schema") or schema or "",
            })
        return {"table": table, "schema": schema or "",
                "foreign_keys": out}

    async def _pg_insert_pk(self, conn, sql_template: str):
        """PG(asyncpg) 不填充 result.lastrowid，这里解析 INSERT 目标表并反射其主键，
        返回主键列名列表；无法解析或语句已自带 RETURNING 时返回 None。"""
        m = re.match(r"\s*INSERT\s+INTO\s+([^\s(]+)", sql_template, re.IGNORECASE)
        if not m or re.search(r"\bRETURNING\b", sql_template, re.IGNORECASE):
            return None  # 无匹配表名，或用户已自行 RETURNING，无需兜底
        # 去掉标识符引用符号（`, ", [ ]），支持 schema.table
        raw = re.sub(r"[`\"\[\]]", "", m.group(1))
        schema, table = (raw.split(".", 1) if "." in raw else (None, raw))
        insp = await conn.run_sync(lambda c: __import__("sqlalchemy").inspect(c))
        pk = await conn.run_sync(
            lambda c: insp.get_pk_constraint(table, schema=schema or None))
        return (pk or {}).get("constrained_columns") or None

    async def exec_sql(self, sql_template: str, params: dict, method: str,
                       page=None, size=None) -> Any:
        # 归一化：去掉首尾空白与结尾分号，便于派生表包裹（分页/内存安全路径）
        sql_template = (sql_template or "").strip().rstrip(";").strip()
        self._validate_sql(sql_template)
        # MySQL LIMIT/OFFSET 不接受字符串参数（'1' 会触发 1064），强制转 int
        params = _coerce_limit_offset_params(sql_template, params or {})
        write, read = self._engines()
        if method == "GET":
            eng = read or write
            async with eng.connect() as conn:
                # ---- 显式分页：SQL 已带 :pageSize/:offset（或 LIMIT/TOP），直接运行 ----
                # 生成器生成的 SELECT 走这里；框架 ?page=&size= 或友好的 :pageIndex 都可换算注入。
                if _has_explicit_pagination(sql_template, self.source_type):
                    p = dict(params or {})
                    eff_size = int(p.get("pageSize") or settings.DEFAULT_PAGE_SIZE)
                    if "pageSize" not in p and page is not None:
                        eff_size = int(size) if size is not None else settings.DEFAULT_PAGE_SIZE
                    eff_size = max(1, min(eff_size, settings.MAX_PAGE_SIZE))
                    p["pageSize"] = eff_size
                    if self.source_type != "sqlserver":
                        if "offset" not in p:
                            if page is not None:
                                p["offset"] = (max(1, int(page)) - 1) * eff_size
                            elif "pageIndex" in p:
                                p["offset"] = (int(p.get("pageIndex") or 1) - 1) * eff_size
                            else:
                                p["offset"] = 0
                    p.pop("pageIndex", None)  # pageIndex 仅用于换算 offset，不接入库
                    base = _strip_pagination(sql_template, self.source_type)
                    # COUNT 子查询已剥掉 LIMIT/TOP，分页参数不再被引用；
                    # 过滤掉避免 SQLAlchemy 报「binds given not in SQL」。
                    count_params = {k: v for k, v in p.items()
                                    if k not in ("pageSize", "offset", "pageIndex")}
                    try:
                        total = await conn.scalar(
                            _compiled(f"SELECT COUNT(*) FROM ({base}) AS _pg"), count_params)
                    except Exception:
                        total = None
                    total = int(total or 0)
                    result = await conn.execute(_compiled(sql_template), p)
                    rows = result.mappings().all()
                    pages = (total + eff_size - 1) // eff_size if eff_size else 1
                    return {"items": [dict(r) for r in rows], "total": total,
                            "page": page, "size": eff_size, "pages": pages}
                # ---- 框架分页（?page=&size=）：仅对未自带分页的 SQL 包裹 ----
                if page is not None:
                    try:
                        page = max(1, int(page))
                    except (TypeError, ValueError):
                        page = 1
                    try:
                        size = int(size) if size is not None else settings.DEFAULT_PAGE_SIZE
                    except (TypeError, ValueError):
                        size = settings.DEFAULT_PAGE_SIZE
                    # M-5 分页边界：size 钳制到 [1, MAX_PAGE_SIZE]，越界不报错只截断
                    size = max(1, min(int(size), settings.MAX_PAGE_SIZE))
                    offset = (page - 1) * size
                    # 真实总量用 COUNT 子查询，不加载全量（内存安全）
                    total = await conn.scalar(
                        _compiled(f"SELECT COUNT(*) FROM ({sql_template}) AS _pg"), params)
                    total = int(total or 0)
                    page_sql = _paginate_sql(sql_template, size, offset, self.source_type)
                    result = await conn.execute(_compiled(page_sql), params)
                    rows = result.mappings().all()
                    pages = (total + size - 1) // size if size else 1
                    return {"items": [dict(r) for r in rows], "page": page,
                            "size": size, "total": total, "pages": pages}
                # ---- 默认：内存安全——只取前 MAX_ROWS+1 行判定是否超量，
                #      绝不一次性把全量结果载入内存（A2）；同时 COUNT 子查询给出
                #      真实 total（同样不加载全量，避免内存爆掉）。
                total = await conn.scalar(
                    _compiled(f"SELECT COUNT(*) FROM ({sql_template}) AS _pg"), params)
                total = int(total or 0)
                # 方言感知封顶：SQL Server 不支持 LIMIT，走 OFFSET...FETCH
                # （与 preview_sql 一致）；之前手写的 LIMIT 会让所有 SQL Server
                # 不带 ?page= 的 GET 接口 500。
                safe_sql = _paginate_sql(
                    sql_template, settings.CUSTOM_SQL_MAX_ROWS + 1, 0, self.source_type)
                result = await conn.execute(_compiled(safe_sql), params)
                rows = result.mappings().all()
                capped = len(rows) > settings.CUSTOM_SQL_MAX_ROWS
                if capped:
                    rows = rows[: settings.CUSTOM_SQL_MAX_ROWS]
                return {"items": [dict(r) for r in rows], "capped": capped,
                        "total": total}
        async with write.begin() as conn:
            sql_to_run = sql_template
            returning_pk = None
            # PG(asyncpg) 不填充 result.lastrowid（恒为 -1/None），用 RETURNING 主键兜底返回自增 id
            if self.source_type == "postgresql" and _is_insert(sql_template):
                try:
                    returning_pk = await self._pg_insert_pk(conn, sql_template)
                except Exception:
                    returning_pk = None
            if returning_pk:
                sql_to_run = (sql_template.rstrip(";").rstrip()
                              + " RETURNING "
                              + ", ".join('"' + c + '"' for c in returning_pk))
            result = await conn.execute(_compiled(sql_to_run), params)
            rowcount = result.rowcount
            lastrowid = getattr(result, "lastrowid", None)
            if returning_pk and (lastrowid is None or lastrowid == -1):
                try:
                    rec = result.mappings().first()
                    if rec:
                        vals = [rec.get(c) for c in returning_pk]
                        # 单主键→标量；复合主键→字典
                        lastrowid = vals[0] if len(vals) == 1 else {c: rec.get(c) for c in returning_pk}
                except Exception:
                    pass
            return {"rowcount": rowcount, "lastrowid": lastrowid}

    @staticmethod
    def _validate_sql(sql: str) -> None:
        s = sql.strip().rstrip(";").strip()
        if not s:
            raise ValueError("SQL 不能为空")
        # 先去掉字符串字面量与注释，避免其中的 ';' 被误判为多语句（避坑 #29 误杀）
        safe = _strip_string_literals(s)
        # 禁止多语句串联
        if ";" in safe:
            raise ValueError("只允许单条 SQL 语句，禁止 ';' 串联多语句（避坑 #29）")
        # 禁止 DDL
        first = re.split(r"\s+", safe, maxsplit=1)[0].upper()
        if first in _DDL_KEYWORDS:
            raise ValueError(f"禁止执行 DDL（{first}），只允许 DML/DQL（避坑 #29）")


register("mysql", RelationalAdapter)
register("postgresql", RelationalAdapter)
register("sqlserver", RelationalAdapter)
