"""管理后台鉴权（库内单一账户 + 首次初始化，见避坑 #21/#27/#30）。

- 首次部署无 admin_account → 强制初始化
- 登录签发无状态 JWT（密钥走环境变量，天然跨 worker，避坑 #27）
- 登录限频 + 失败锁定，防暴破（避坑 #30）
- 口令 bcrypt 哈希存储
"""
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import jwt
import bcrypt

from .config import settings
from .cache import cache
from .metadata_db import session_scope
from .models import AdminAccount

# 直接用 bcrypt 库（避免 passlib 与新版 bcrypt 的兼容性问题）。
def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False

ALGO = "HS256"


async def needs_init() -> bool:
    async with session_scope() as s:
        from sqlalchemy import select, func
        n = (await s.execute(select(func.count()).select_from(AdminAccount))).scalar()
        return (n or 0) == 0


async def init_admin(password: str) -> None:
    h = _hash(password)
    async with session_scope() as s:
        s.add(AdminAccount(password_hash=h))


async def verify_password(password: str) -> bool:
    async with session_scope() as s:
        from sqlalchemy import select
        acc = (await s.execute(select(AdminAccount).limit(1))).scalar_one_or_none()
        if not acc:
            return False
        return _verify(password, acc.password_hash)


def create_token(pc: int = None) -> str:
    exp = int(time.time()) + settings.SESSION_TTL_SECONDS
    payload = {"sub": "admin", "exp": exp}
    # pc = 口令变更时间戳（password changed at）：改口令后旧令牌因 pc 不匹配被拒。
    # 仅当显式传入时才写入声明；不带 pc 的令牌按「旧令牌」处理（升级前已签发的会话仍有效）。
    if pc is not None:
        payload["pc"] = pc
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGO)


async def verify_token(token: str) -> bool:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGO])
    except Exception:
        return False
    pc = payload.get("pc")
    if pc is None:
        # 旧令牌（无 pc 声明）仍视为有效，直到自然过期（兼容升级前的会话）
        return True
    # 与库中口令变更时间比对：改口令后旧 pc 失效。
    # pwd_changed_at 短缓存（~5s）：admin 面板每次请求都要鉴权，逐次查库成本高；
    # 改口令后最多 5s 内旧令牌仍可能被放行（有界延迟），属可接受的安全权衡。
    try:
        cur = await _get_pwd_changed_at()
        if cur is None:
            return True
        return pc == cur
    except Exception:
        # 数据库异常时 fail-closed：拒绝验证，避免攻击者借 DB 压力放行旧/伪造令牌
        logging.warning("Token pc 比对 DB 查询失败，按 fail-closed 拒绝")
        return False


_PWD_CHANGED_TTL = 5  # 秒；改口令后旧令牌最多 5s 内仍可能被放行（有界延迟）


async def _get_pwd_changed_at() -> Optional[int]:
    """读取 admin 账户的口令变更时间戳（int 秒），带短 TTL 缓存，避免逐请求打元库。"""
    cached = await cache.get("auth:pwd_changed_at")
    if cached is not None:
        try:
            # 缓存命中：哨兵 "0" 表示「从未改密」（None），否则为时间戳
            return None if cached == "0" else int(cached)
        except (TypeError, ValueError):
            pass  # 缓存值损坏，回落到查库
    from sqlalchemy import select
    async with session_scope() as s:
        acc = (await s.execute(select(AdminAccount).limit(1))).scalar_one_or_none()
    ts = (int(acc.pwd_changed_at.replace(tzinfo=timezone.utc).timestamp())
          if acc and acc.pwd_changed_at is not None else None)
    # 即便为 None 也缓存（用 "0" 哨兵），避免反复查空库
    await cache.set("auth:pwd_changed_at", "0" if ts is None else str(ts),
                    ttl=_PWD_CHANGED_TTL)
    return ts


# ---- 登录防暴破（避坑 #30）----
async def login_allowed(ip: str) -> bool:
    if await cache.get(f"login:lock:{ip}"):
        return False
    fails = await cache.get(f"login:fail:{ip}")
    if fails and int(fails) >= settings.LOGIN_RATE_LIMIT_PER_MIN:
        return False
    return True


async def report_login(ip: str, success: bool) -> None:
    if success:
        await cache.delete(f"login:fail:{ip}")
        await cache.delete(f"login:lock:{ip}")
        return
    n = await cache.incr(f"login:fail:{ip}", ttl=120)
    if n >= settings.LOGIN_LOCKOUT_FAILURES:
        await cache.set(f"login:lock:{ip}", "1", ttl=settings.LOGIN_LOCKOUT_SECONDS)
