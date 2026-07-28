"""SQLAlchemy 2.0 元数据模型 + Pydantic schema。

表：
- data_sources      数据源连接配置（config 含 AES 加密后的密码）
- api_definitions   接口定义（均为自定义 SQL 接口，含发布状态与覆盖）
- api_logs          详细调用日志（按天分区，主键含分区键 log_date）
- daily_stats       每日调用聚合
- admin_account     单一管理员账户（口令哈希）
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    JSON,
    Boolean,
    Date,
    BigInteger,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # mysql/sqlserver/postgresql
    config: Mapped[dict] = mapped_column(JSON, nullable=False)      # 密码已 AES 加密
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ApiDefinition(Base):
    __tablename__ = "api_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="custom")  # 固定为 custom（自定义 SQL 接口）
    resource: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 预留字段（当前未使用）
    base_path: Mapped[str] = mapped_column(String(256), nullable=False)
    methods: Mapped[str] = mapped_column(String(64), default="GET")  # 逗号分隔
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 接口用途备注
    tag: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 分组/标签（用于列表筛选）
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)     # custom 的 SQL 模板
    params: Mapped[dict] = mapped_column(JSON, default=dict)         # custom 入参声明
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)      # 覆盖（缓存/校验）
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                onupdate=func.now())


class ApiLog(Base):
    __tablename__ = "api_logs"

    # 复合主键 (id, log_date)：log_date 必须在主键内以满足 MySQL RANGE 分区要求；
    # id 用 UUID 字符串（跨方言一致，SQLite 回退也支持，避免自增复合主键在 SQLite 报错）。
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    log_date: Mapped[datetime] = mapped_column(DateTime, primary_key=True,
                                               server_default=func.now())
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, default=_uuid)
    api_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    query_params: Mapped[dict] = mapped_column(JSON, default=dict)   # 已脱敏
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_size: Mapped[int] = mapped_column(Integer, default=0)
    response_size: Mapped[int] = mapped_column(Integer, default=0)
    caller_ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(256), default="")
    error_type: Mapped[str] = mapped_column(String(64), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")      # 已脱敏
    # 缓存命中标记：命中只读响应缓存置 True（仅 GET 命中缓存路径），用于缓存命中率指标
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class DailyStat(Base):
    __tablename__ = "daily_stats"

    api_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    day: Mapped[datetime] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(BigInteger, default=0)
    errors: Mapped[int] = mapped_column(BigInteger, default=0)


class AdminAccount(Base):
    __tablename__ = "admin_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 口令变更时间：写入 JWT（pc 声明），改口令后旧令牌因 pc 不匹配被拒，实现「改密即失效」
    pwd_changed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now())


class SystemSetting(Base):
    """系统设置键值表（运行时配置持久化，重启后由 load_runtime_settings 读回）。"""
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")  # JSON 文本
