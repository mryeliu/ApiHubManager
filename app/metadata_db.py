"""元数据库连接与初始化（MySQL 生产 / SQLite 回退）。

- 生产：METADATA_DB_URL=mysql+aiomysql://...
- 回退：USE_SQLITE=1 或 未提供 METADATA_DB_URL → sqlite+aiosqlite
建表后，若为 MySQL 则把 api_logs 转换为按天 RANGE 分区（见避坑 #16）。
"""
import asyncio
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text, inspect, make_url
from sqlalchemy.sql.elements import ClauseElement

from .config import settings
from .models import Base, ApiLog


def _server_default_sql(col, dialect) -> str:
    """把 Column.server_default 渲染成 DDL 片段（如 " DEFAULT CURRENT_TIMESTAMP"）。

    兼容 SQLAlchemy 2.0：server_default 是 DefaultClause，真正的 SQL 表达式在
    .arg 上；直接 DefaultClause.compile() 在新版本会抛 AttributeError。

    坑（SQLite）：ALTER TABLE ADD COLUMN 不允许“非恒定默认值”（如
    CURRENT_TIMESTAMP / now() 这类函数），会报
    "Cannot add a column with non-constant default" 并导致整列加不上。
    因此在 SQLite 下，凡是表达式型 server_default 一律省略 DEFAULT（列均可空，
    历史行取 NULL，新行由 ORM 在 INSERT 时按模型 server_default 填值）；
    MySQL 允许表达式默认，保留。
    """
    sd = col.server_default
    if sd is None:
        return ""
    arg = getattr(sd, "arg", None)
    if arg is None:
        return ""
    is_sqlite = getattr(dialect, "name", "") == "sqlite"
    if isinstance(arg, ClauseElement):
        if is_sqlite:
            return ""  # 省略非恒定默认，避免 SQLite ALTER 失败
        try:
            rendered = str(arg.compile(dialect=dialect))
        except Exception:
            rendered = str(arg)
        return " DEFAULT " + rendered
    return " DEFAULT " + str(arg)


def _build_url() -> str:
    if settings.METADATA_DB_URL:
        return settings.METADATA_DB_URL
    if settings.USE_SQLITE:
        return f"sqlite+aiosqlite:///{settings.SQLITE_PATH}"
    # 默认回退 SQLite，保证可本地启动
    return f"sqlite+aiosqlite:///{settings.SQLITE_PATH}"


URL = _build_url()
IS_MYSQL = URL.startswith("mysql")

# 连接池配置（M-26）：pool_recycle 防止 MySQL 长时间空闲连接被服务端断开。
# 注意：SQLite 用 StaticPool，不支持 pool_size/max_overflow 参数，必须按方言区分。
_pool_kwargs = {"echo": False, "pool_pre_ping": True, "future": True}
if not URL.startswith("sqlite"):
    _pool_kwargs.update(pool_recycle=1800, pool_size=10, max_overflow=20)
engine = create_async_engine(URL, **_pool_kwargs)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope():
    """事务作用域：异常自动回滚。"""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """建表 + 补齐缺失列（模型演进，不丢数据）+ （MySQL）转换日志表为按天分区。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _add_missing_columns()
    if IS_MYSQL:
        await _convert_logs_to_partitioned()


def _plan_missing_columns(sync_conn) -> list:
    """在同步连接内用 inspect 收集需要新增的列（仅读，规避异步/线程混用）。"""
    inspector = inspect(sync_conn)
    plan = []
    for table in Base.metadata.tables.values():
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            default = ""
            if col.server_default is not None:
                default = _server_default_sql(col, sync_conn.dialect)
            elif not col.nullable:
                default = " NOT NULL"
            plan.append((table.name, col.name,
                         str(col.type.compile(dialect=sync_conn.dialect)), default))
    return plan


def _sqlite_default_for(col) -> str:
    """SQLite 不允许给已有数据的表 ALTER 新增 NOT NULL 列；给缺失的
    NOT NULL 列补一个空默认值，保证 ALTER 成功（已存在行取该默认值）。
    """
    t = str(col.type.compile(dialect=engine.dialect)).upper()
    if "INT" in t or "BOOLEAN" in t or "DATETIME" in t or "DATE" in t:
        return " DEFAULT 0"
    if "JSON" in t:
        return " DEFAULT '{}'"
    return " DEFAULT ''"  # VARCHAR / TEXT 等


async def _add_missing_columns() -> None:
    """create_all 不会给已存在的表追加新列；按模型补齐缺失列（幂等、不丢数据）。

    适用于开发期持续给模型加字段（如 ApiDefinition.remark），避免手动迁移。

    坑：SQLite 给“已有数据的表” ALTER ADD 一个 NOT NULL 列会直接报
    “Cannot add a NOT NULL column with default value NULL”。因此：
      - 模型里后加的列都应声明 nullable=True（如 remark）；
      - 若确有 NOT NULL 缺失列，这里自动补一个空默认值让 ALTER 成功。
    MySQL 是服务端库，无此限制，走异步引擎即可。
    """
    if IS_MYSQL:
        async with engine.begin() as conn:
            plan = await conn.run_sync(_plan_missing_columns)
            for table_name, col_name, col_type, default in plan:
                ddl = (f"ALTER TABLE {table_name} ADD COLUMN {col_name} "
                       f"{col_type}{default}")
                try:
                    await conn.execute(text(ddl))
                except Exception:
                    pass
        return

    # SQLite：原生 sqlite3 连接直接 ALTER + commit（规避异步连接池的提交/锁问题）
    import sqlite3
    import pathlib
    db_path = make_url(URL).database or settings.SQLITE_PATH
    db_path = pathlib.Path(db_path).resolve()
    raw = sqlite3.connect(str(db_path), timeout=30)
    try:
        cur = raw.cursor()
        for table in Base.metadata.tables.values():
            cur.execute(f"PRAGMA table_info({table.name})")
            existing = {r[1] for r in cur.fetchall()}
            for col in table.columns:
                if col.name in existing:
                    continue
                default = ""
                if col.server_default is not None:
                    default = _server_default_sql(col, engine.dialect)
                elif not col.nullable:
                    default = _sqlite_default_for(col)
                ddl = (f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                       f"{col.type.compile(dialect=engine.dialect)}{default}")
                try:
                    cur.execute(ddl)
                except Exception:
                    # 方言不支持或已存在，忽略（运维可手动补）
                    pass
        raw.commit()
    finally:
        raw.close()


async def _convert_logs_to_partitioned() -> None:
    """把 api_logs 改为按天 RANGE 分区（幂等）。"""
    ddl = (
        "ALTER TABLE api_logs PARTITION BY RANGE (TO_DAYS(log_date)) ("
        "PARTITION p_init VALUES LESS THAN (MAXVALUE))"
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text(ddl))
    except Exception:
        # 已分区或暂不支持，忽略（运维任务会补齐分区）
        pass


async def dispose() -> None:
    await engine.dispose()
