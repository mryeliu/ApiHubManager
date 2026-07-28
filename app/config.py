"""全局配置：全部来自环境变量（配置外置，见避坑清单 #18）。

生产形态 = MySQL 元数据库 + Redis 缓存/协调（多 worker FastAPI）。
为便于本地验证，当未提供 METADATA_DB_URL / REDIS_URL 时自动回退到
SQLite（元数据）与进程内内存缓存（仅开发/测试用，生产不可用）。
"""
import os
from pathlib import Path
from typing import List

# 本地开发便利：启动前自动读取项目根目录的 .env（若存在）；
# .env.local 用于本地覆盖（例如 USE_SQLITE=1 / USE_MEMORY_CACHE=1）。
# 路径基于本文件定位项目根，确保无论从哪个目录启动都能加载到。
# 容器内/生产环境通常没有这两个文件，静默跳过，不影响 Docker 部署。
try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
    load_dotenv(_PROJECT_ROOT / ".env.local", override=True)
except Exception:
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "1" if default else "0").strip().lower()
    if val in ("1", "true", "yes", "on", "y", "t"):
        return True
    if val in ("0", "false", "no", "off", "n", "f", ""):
        return False
    # 未知值回退到 default，避免误判
    return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


class Settings:
    # ---- 服务 ----
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = _int("PORT", 8000)

    # ---- 元数据库（MySQL 生产 / SQLite 回退）----
    # 生产置 METADATA_DB_URL=mysql+aiomysql://user:pass@host:3306/api_mgr
    METADATA_DB_URL: str = os.getenv("METADATA_DB_URL", "")
    USE_SQLITE: bool = _bool("USE_SQLITE", False)
    SQLITE_PATH: str = os.getenv("SQLITE_PATH", "metadata.db")

    # ---- Redis（生产 / 内存回退）----
    REDIS_URL: str = os.getenv("REDIS_URL", "")
    USE_MEMORY_CACHE: bool = _bool("USE_MEMORY_CACHE", False)

    # ---- 鉴权（管理后台，库内单一账户 + 首次初始化）----
    JWT_SECRET: str = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PROD_JWT_SECRET")
    SESSION_TTL_SECONDS: int = _int("SESSION_TTL_SECONDS", 1800)  # 30 分钟
    # 管理后台会话 Cookie 是否标记 Secure（仅 HTTPS 传输）。
    # 本地 http 开发置 0；生产前置 TLS 后务必置 1（见避坑 #31）。
    COOKIE_SECURE: bool = _bool("COOKIE_SECURE", False)
    LOGIN_RATE_LIMIT_PER_MIN: int = _int("LOGIN_RATE_LIMIT_PER_MIN", 5)
    LOGIN_LOCKOUT_FAILURES: int = _int("LOGIN_LOCKOUT_FAILURES", 5)
    LOGIN_LOCKOUT_SECONDS: int = _int("LOGIN_LOCKOUT_SECONDS", 300)

    # ---- 密钥（数据源密码 AES 加密，见避坑 #28 / 假设 A14）----
    DB_SECRET_KEY: str = os.getenv("DB_SECRET_KEY", "CHANGE_ME_DB_SECRET_32BYTES_MIN")

    # ---- 限流（业务面，默认开启，跨进程一致）----
    RL_ENABLED: bool = _bool("RL_ENABLED", True)
    RL_IP_LIMIT: int = _int("RL_IP_LIMIT", 100)      # 单 IP 每分钟
    RL_API_LIMIT: int = _int("RL_API_LIMIT", 1000)   # 单接口每分钟

    # ---- 熔断（数据源级）----
    CB_FAILURE_THRESHOLD: int = _int("CB_FAILURE_THRESHOLD", 5)
    CB_COOLDOWN_SECONDS: int = _int("CB_COOLDOWN_SECONDS", 30)

    # ---- 日志（异步队列 + 批量 + 采样 + 留存）----
    LOG_RETENTION_DAYS: int = _int("LOG_RETENTION_DAYS", 3)
    # 日志留存清理周期（秒）。默认 600（10 分钟），比原先的 1 小时更细；
    # 也可在「系统设置」保存留存天数时立即触发一次。可用环境变量 LOG_MAINTENANCE_INTERVAL 调整。
    LOG_MAINTENANCE_INTERVAL: int = _int("LOG_MAINTENANCE_INTERVAL", 600)
    LOG_BATCH_SIZE: int = _int("LOG_BATCH_SIZE", 200)
    LOG_FLUSH_INTERVAL: float = _float("LOG_FLUSH_INTERVAL", 1.0)
    LOG_QUEUE_MAX: int = _int("LOG_QUEUE_MAX", 5000)

    # ---- 高量采样 ----
    SAMPLE_QPS_THRESHOLD: int = _int("SAMPLE_QPS_THRESHOLD", 50)
    SAMPLE_RATIO: float = _float("SAMPLE_RATIO", 0.1)
    SLOW_MS: int = _int("SLOW_MS", 1000)

    # ---- 自定义 SQL 加固（避坑 #29）----
    CUSTOM_SQL_MAX_ROWS: int = _int("CUSTOM_SQL_MAX_ROWS", 1000)

    # ---- 数据源连接池（每个 worker 进程内，按 source_id 缓存引擎）----
    # 默认 pool_size=10，max_overflow=20：单 worker 最多 30 并发连接/数据源，
    # 4 worker 共 120 条。高并发场景可按 DB 侧 max_connections 上调。
    DB_POOL_SIZE: int = _int("DB_POOL_SIZE", 10)
    DB_MAX_OVERFLOW: int = _int("DB_MAX_OVERFLOW", 20)

    # ---- 分页 ----
    DEFAULT_PAGE_SIZE: int = _int("DEFAULT_PAGE_SIZE", 20)
    MAX_PAGE_SIZE: int = _int("MAX_PAGE_SIZE", 200)

    # ---- A1：只读响应缓存（Redis / 内存回退，按 SQL+参数 命中）----
    API_CACHE_ENABLED: bool = _bool("API_CACHE_ENABLED", True)
    API_CACHE_TTL: int = _int("API_CACHE_TTL", 60)  # 命中缓存的存活秒数

    # ---- A3：响应压缩（Gzip/Brotli）----
    COMPRESS_RESPONSES: bool = _bool("COMPRESS_RESPONSES", True)
    COMPRESS_MIN_SIZE: int = _int("COMPRESS_MIN_SIZE", 1024)  # 小于此字节数不压缩

    # ---- CORS（业务面，浏览器侧；见系统设置卡片③）----
    CORS_ALLOW_ORIGINS: List[str] = [
        o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()
    ]
    CORS_ALLOW_CREDENTIALS: bool = _bool("CORS_ALLOW_CREDENTIALS", False)

    # ---- 杂项 ----
    API_PREFIX: str = os.getenv("API_PREFIX", "/api/v1")
    DOCS_ENABLED: bool = _bool("DOCS_ENABLED", False)


settings = Settings()

# 生产形态强制密钥：配置了远程元数据库或 Redis 即视为生产部署，
# 必须用强随机密钥覆盖默认占位值，否则拒绝启动（C-3/C-4）。
# 纯本地开发（USE_SQLITE + 内存缓存）允许使用占位值，方便快速起服务。
def _is_production_shape() -> bool:
    return bool(settings.METADATA_DB_URL) or (
        bool(settings.REDIS_URL) and not settings.USE_MEMORY_CACHE
    )

if _is_production_shape():
    _weak_secrets = {
        "CHANGE_ME_IN_PROD_JWT_SECRET",
        "CHANGE_ME_DB_SECRET_32BYTES_MIN",
    }
    if not settings.JWT_SECRET or settings.JWT_SECRET in _weak_secrets:
        raise RuntimeError(
            "生产部署必须设置强随机 JWT_SECRET 环境变量（默认占位值不安全，可被伪造管理员 Token）"
        )
    if not settings.DB_SECRET_KEY or settings.DB_SECRET_KEY in _weak_secrets:
        raise RuntimeError(
            "生产部署必须设置强随机 DB_SECRET_KEY 环境变量（默认占位值不安全，可解密所有数据源密码）"
        )
