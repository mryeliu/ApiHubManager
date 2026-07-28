"""接口发布与路由热更新（避坑 #5）。

不逐个往 app 注册路由，而是维护一份进程内 REGISTRY，由单一 catch-all 路由分发；
发布/停用写 DB 后递增 route:version，后台轮询检测到变化即重载 REGISTRY，
从而实现多 worker 热更新（不重启）。
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, Request
from sqlalchemy import select

from .config import settings
from .cache import cache
from .metadata_db import session_scope
from .models import DataSource, ApiDefinition
from .sources.base import get_adapter_class

REGISTRY: dict[tuple[str, str], "ApiEntry"] = {}
_CURRENT_VERSION = -1


@dataclass
class ApiEntry:
    api_id: str
    kind: str
    resource: Optional[str]
    methods: list[str]
    source_id: str
    source_name: str
    source_type: str
    source_config: dict
    sql: Optional[str] = None
    params: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)
    adapter: object = None


async def reload() -> None:
    """从 DB 重新加载已发布接口到 REGISTRY（跨 worker 真相在 MySQL）。"""
    global REGISTRY, _CURRENT_VERSION
    async with session_scope() as s:
        dss = (await s.execute(select(DataSource))).scalars().all()
        src_map = {d.id: d for d in dss}
        apis = (await s.execute(
            select(ApiDefinition).where(ApiDefinition.published == True)  # noqa: E712
        )).scalars().all()
        new: dict[tuple[str, str], ApiEntry] = {}
        for a in apis:
            d = src_map.get(a.source_id)
            if not d:
                # 已发布但关联数据源不存在（如被删除/切换库）→ 无法注册，调用会 404。
                # 发布入口已拦截（publish 校验数据源），此处仅为兜底告警。
                logging.warning(
                    "已发布接口 %s 关联的数据源 %s 不存在，将跳过注册（调用会 404）。"
                    "请重建数据源或重新选择数据源后重新发布。", a.id, a.source_id)
                continue
            # URL 路径段：统一使用 base_path（系统仅支持自定义 SQL 接口）
            seg = a.base_path
            if not seg:
                # 缺少路由路径段（异常记录），跳过以免崩溃
                continue
            cls = get_adapter_class(d.type)
            adapter = cls(d.id, d.type, d.config)
            key = (d.name.lower(), seg.lower())
            if key in new:
                # 同名接口会互相覆盖（后加载者优先），否则难以排查「改了 A 却生效的是 B」
                logging.warning(
                    "接口路径冲突：'%s/%s' 已被接口 %s 占用，接口 %s 将覆盖它（后加载者优先）。"
                    "请修改其中一个的 source 名称或路径段以避免互相覆盖。",
                    d.name.lower(), seg.lower(), new[key].api_id, a.id,
                )
            new[key] = ApiEntry(
                api_id=a.id,
                kind=a.kind,
                resource=a.resource,
                methods=[m.strip().upper() for m in a.methods.split(",")],
                source_id=d.id,
                source_name=d.name,
                source_type=d.type,
                source_config=d.config,
                sql=a.sql,
                params=a.params or {},
                overrides=a.overrides or {},
                adapter=adapter,
            )
    REGISTRY = new
    _CURRENT_VERSION = await cache.get_route_version()


async def maybe_reload() -> None:
    """后台轮询：版本变化才重载。"""
    global _CURRENT_VERSION
    v = await cache.get_route_version()
    if v != _CURRENT_VERSION:
        await reload()


def get_entry(source: str, resource: str) -> Optional[ApiEntry]:
    return REGISTRY.get((source.lower(), resource.lower()))


router = APIRouter()


@router.api_route(
    f"{settings.API_PREFIX}/{{subpath:path}}",
    methods=["GET", "POST", "PUT", "DELETE"],
)
async def dispatch(subpath: str, request: Request = None):
    from .business import execute
    segs = [s for s in subpath.split("/") if s != ""]
    if not segs:
        # 空路径：交回 execute 走统一的 404
        return await execute("", "", None, request)
    # 路径段可含斜杠：base_path（自定义 SQL 接口）可能多段、也可带末尾 id。
    # 枚举所有「数据源名 / 资源」切分点，命中 REGISTRY 即分派。
    n = len(segs)
    for i in range(1, n):           # i 段作为数据源名（至少 1 段）
        source = "/".join(segs[:i])
        rest = segs[i:]
        # 候选 1：整段 rest 作为资源（custom 的 base_path 可含 /，无 rid）
        full = "/".join(rest)
        if get_entry(source, full):
            return await execute(source, full, None, request)
        # 候选 2：rest[:-1] 为资源、rest[-1] 为 rid（自定义 SQL 接口路径可带末尾 id）
        if len(rest) >= 2:
            res = "/".join(rest[:-1])
            if get_entry(source, res):
                return await execute(source, res, rest[-1], request)
    # 无任何匹配：用第一段当作数据源名，交回 execute 走 404
    return await execute(segs[0], "", None, request)
