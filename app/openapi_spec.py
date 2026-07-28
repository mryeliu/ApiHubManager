"""为已发布自定义接口生成 OpenAPI 3.0 文档。

消费端（前端）可据此直接对接，不再只给裸路径。参数来自接口的「入参声明」，
响应结构因任意 SQL 结果未知，给通用 object。
"""
import re

from sqlalchemy import select

from .config import settings
from .metadata_db import session_scope
from .models import ApiDefinition, DataSource
from .business import _normalize_params


def _oa_type(typ: str) -> str:
    t = (typ or "string").lower()
    if t in ("int", "integer"):
        return "integer"
    if t in ("float", "number", "decimal"):
        return "number"
    if t in ("bool", "boolean"):
        return "boolean"
    return "string"


_VALID_IN = {"query", "header", "path", "cookie"}


def _path_item(a, params, method):
    """按方法生成 path item：GET 用 query 参数；POST/PUT/DELETE 用 JSON requestBody。

    运行时对 GET 读 query string、对写方法读 JSON body，文档需与实际收参方式一致，
    否则 Swagger/代码生成工具会给出错误的请求形态。
    """
    lower = (method or "GET").lower()
    item = {
        "summary": a.name,
        "description": a.remark or "",
    }
    if lower == "get":
        item["parameters"] = [
            {
                "name": p["name"],
                "in": (p["in"] if p["in"] in _VALID_IN else "query") or "query",
                "required": bool(p["required"]),
                "schema": {"type": _oa_type(p["type"])},
                "description": p["desc"],
            }
            for p in params
        ]
    else:
        required_names = [p["name"] for p in params if p["required"]]
        item["requestBody"] = {
            "required": bool(required_names) or bool(params),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            p["name"]: {
                                "type": _oa_type(p["type"]),
                                "description": p["desc"],
                            }
                            for p in params
                        },
                        "required": required_names,
                    }
                }
            },
        }
    item["responses"] = {
        "200": {
            "description": "成功",
            "content": {"application/json": {"schema": {"type": "object"}}},
        },
        "400": {"description": "参数错误或缺少必填入参"},
        "405": {"description": "方法不允许"},
        "429": {"description": "请求过于频繁"},
        "500": {"description": "执行失败"},
    }
    return item


def _doc(paths, title):
    return {
        "openapi": "3.0.3",
        "info": {
            "title": title,
            "version": "1.0.0",
            "description": "由系统根据自定义接口自动生成",
        },
        "servers": [{"url": "/"}],
        "paths": paths,
    }


async def build_openapi() -> dict:
    async with session_scope() as s:
        dss = (await s.execute(select(DataSource))).scalars().all()
        src_map = {d.id: d for d in dss}
        apis = (await s.execute(
            select(ApiDefinition).where(ApiDefinition.published == True)  # noqa: E712
        )).scalars().all()

    paths: dict = {}
    for a in apis:
        d = src_map.get(a.source_id)
        if not d:
            continue
        if a.kind != "custom":
            continue
        if not a.base_path:
            continue
        # 路径需与 publisher 分发时一致：source 名与 base_path 均小写，否则文档路径 404
        path = re.sub(r"/+", "/", f"{settings.API_PREFIX}/{d.name.lower()}/{a.base_path.lower()}")
        methods = [m.strip().upper() for m in a.methods.split(",") if m.strip()]
        params = _normalize_params(a.params)
        for m in methods:
            paths.setdefault(path, {})[m.lower()] = _path_item(a, params, m)
    return _doc(paths, "API 管理系统 - 已发布接口")


async def build_openapi_for(api_id: str):
    """生成单个接口的 OpenAPI 文档（不限是否发布，便于发布前预览；支持 custom）。"""
    async with session_scope() as s:
        a = (await s.execute(
            select(ApiDefinition).where(ApiDefinition.id == api_id)
        )).scalars().first()
        if not a:
            return None
        ds = (await s.execute(
            select(DataSource).where(DataSource.id == a.source_id)
        )).scalars().first()
        if not ds:
            return None

    paths: dict = {}
    if a.kind == "custom":
        if not a.base_path:
            return None
        # 路径需与 publisher 分发时一致：source 名与 base_path 均小写，否则文档路径 404
        path = re.sub(r"/+", "/", f"{settings.API_PREFIX}/{ds.name.lower()}/{a.base_path.lower()}")
        methods = [m.strip().upper() for m in a.methods.split(",") if m.strip()]
        params = _normalize_params(a.params)
        for m in methods:
            paths.setdefault(path, {})[m.lower()] = _path_item(a, params, m)
    else:
        return None
    return _doc(paths, f"API 管理系统 - {a.name}")
