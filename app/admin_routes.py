"""管理面 REST（数据源 / 接口 / 发布 / 统计 / 日志 / 系统设置 / 登录 / 初始化）。

所有写操作（init/login/me 除外）需校验管理员会话（无状态 JWT，跨 worker）。
密码字段在落库前 AES 加密（避坑 #28）。
"""
import copy
import json
from datetime import datetime, timezone
from typing import Optional, Union

from fastapi import APIRouter, Request, HTTPException, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_

from .config import settings
from .cache import cache
from .metadata_db import session_scope
from .models import DataSource, ApiDefinition, ApiLog, SystemSetting, AdminAccount
from .crypto import encrypt_secret, decrypt_secret
from .auth import (
    needs_init, init_admin, verify_password, create_token, verify_token,
    login_allowed, report_login, _verify,
)
from .sources.base import get_adapter_class
from .sources.sql import dispose_engines
from .sql_rules import validate_api_sql
from .stats import get_overview, get_trend, get_realtime

_runtime_settings = {
    # CORS：cors_enabled=False 时拒绝一切跨域（默认安全）；True 时按 origins 放行
    "cors_enabled": bool(settings.CORS_ALLOW_ORIGINS),
    "cors_allow_origins": settings.CORS_ALLOW_ORIGINS,
    "cors_allow_credentials": settings.CORS_ALLOW_CREDENTIALS,
    "sample_qps_threshold": settings.SAMPLE_QPS_THRESHOLD,
    "sample_ratio": settings.SAMPLE_RATIO,
    "slow_ms": settings.SLOW_MS,
    "log_retention_days": settings.LOG_RETENTION_DAYS,
}

# 持久化键：整份运行时设置存为一行 JSON，重启后回读
_RUNTIME_KEY = "runtime"

_PWD_FIELDS = ("password", "read_password")


def _encrypt_config(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    for k in _PWD_FIELDS:
        if k in out and out[k]:
            out[k] = encrypt_secret(out[k])
    return out


def _mask_config(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    for k in _PWD_FIELDS:
        if k in out and out[k]:
            out[k] = "******"
    return out


def _bearer(request: Request) -> Optional[str]:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None


async def require_admin(request: Request) -> None:
    token = request.cookies.get("admin_token") or _bearer(request)
    if not token or not await verify_token(token):
        raise HTTPException(status_code=401, detail="未登录或会话失效")


async def bump_version() -> None:
    await cache.incr_route_version()


# ---------- 公开（无需会话）----------
open_router = APIRouter(prefix="/admin/api")


class InitReq(BaseModel):
    password: str
    confirm: str


@open_router.get("/me")
async def me():
    return {"initialized": not await needs_init()}


@open_router.post("/init")
async def init(req: InitReq):
    if await needs_init() is False:
        raise HTTPException(status_code=400, detail="已初始化，禁止重复初始化")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="口令至少 6 位")
    if req.password != req.confirm:
        raise HTTPException(status_code=400, detail="两次输入不一致")
    await init_admin(req.password)
    return {"ok": True}


class LoginReq(BaseModel):
    password: str


@open_router.post("/login")
async def login(req: LoginReq, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not await login_allowed(ip):
        raise HTTPException(status_code=429, detail="尝试过于频繁，已临时锁定")
    # 载入账户并校验口令；同时取出 pwd_changed_at 写入令牌（pc），用于改密即失效
    async with session_scope() as s:
        acc = (await s.execute(select(AdminAccount).limit(1))).scalar_one_or_none()
    if not acc or not _verify(req.password, acc.password_hash):
        if acc:
            await report_login(ip, success=False)
        raise HTTPException(status_code=401, detail="口令错误")
    await report_login(ip, success=True)
    pc = int(acc.pwd_changed_at.replace(tzinfo=timezone.utc).timestamp()) if acc.pwd_changed_at else 0
    token = create_token(pc)
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True, "expires_in": settings.SESSION_TTL_SECONDS})
    resp.set_cookie("admin_token", token, httponly=True, samesite="lax",
                    secure=settings.COOKIE_SECURE,
                    max_age=settings.SESSION_TTL_SECONDS, path="/")
    return resp


@open_router.post("/logout")
async def logout():
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True})
    # 清除会话 Cookie（与登录同 path/名，确保覆盖）
    resp.delete_cookie("admin_token", path="/")
    return resp


# ---------- 需会话 ----------
secure_router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])


class DatasourceCreate(BaseModel):
    name: str
    type: str
    config: dict


@secure_router.get("/datasources")
async def list_datasources():
    async with session_scope() as s:
        rows = (await s.execute(select(DataSource))).scalars().all()
        return [{"id": r.id, "name": r.name, "type": r.type,
                 "config": _mask_config(r.config), "status": r.status}
                for r in rows]


@secure_router.post("/datasources")
async def create_datasource(req: DatasourceCreate):
    if req.type not in ("mysql", "postgresql", "sqlserver"):
        raise HTTPException(status_code=400, detail="不支持的数据源类型")
    # 数据源名称唯一（不区分大小写），避免 REGISTRY 按 name.lower() 冲突互相覆盖
    async with session_scope() as s:
        exists = (await s.execute(
            select(DataSource).where(func.lower(DataSource.name) == req.name.lower())
        )).first()
        if exists:
            raise HTTPException(status_code=400, detail="数据源名称已存在")
    cls = get_adapter_class(req.type)
    # 每次测试用唯一 source_id，避免 _ENGINES 缓存了上一次（失败）的引擎导致重试无效
    import uuid as _uuid
    test_id = f"__test_{_uuid.uuid4().hex}"
    adapter = cls(test_id, req.type, _encrypt_config(req.config))
    ok, err = await adapter.test_connection()
    # 无论成功失败，释放临时测试引擎，避免连接池泄漏（L-3/H-3）
    await dispose_engines(test_id)
    if not ok:
        raise HTTPException(status_code=400, detail=f"连接测试失败：{err}")
    async with session_scope() as s:
        ds = DataSource(name=req.name, type=req.type,
                        config=_encrypt_config(req.config), status="ok")
        s.add(ds)
        await s.flush()
        return {"id": ds.id, "name": ds.name, "type": ds.type}


@secure_router.post("/datasources/{ds_id}/test")
async def test_datasource(ds_id: str):
    async with session_scope() as s:
        ds = (await s.execute(select(DataSource).where(DataSource.id == ds_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据源不存在")
        adapter = get_adapter_class(ds.type)(ds.id, ds.type, ds.config)
        ok, err = await adapter.test_connection()
        ds.status = "ok" if ok else "error"
        await s.flush()
        return {"ok": ok, "error": err}


@secure_router.delete("/datasources/{ds_id}")
async def delete_datasource(ds_id: str):
    async with session_scope() as s:
        ds = (await s.execute(select(DataSource).where(DataSource.id == ds_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据源不存在")
        # 级联：先删其接口（再删数据源）
        apis = (await s.execute(
            select(ApiDefinition).where(ApiDefinition.source_id == ds_id))).scalars().all()
        for a in apis:
            await s.delete(a)
        await s.delete(ds)
    # 释放该数据源缓存的引擎连接池，避免连接池泄漏（H-3）
    from .sources.sql import dispose_engines
    await dispose_engines(ds_id)
    await bump_version()
    return {"ok": True}


class DatasourceUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    config: Optional[dict] = None


@secure_router.put("/datasources/{ds_id}")
async def update_datasource(ds_id: str, req: DatasourceUpdate):
    """编辑数据源（改主机/凭据不必删除重建，避免 source_id 变更让已发布接口失效）。

    - name 不区分大小写唯一（排除自身）；
    - type 必须在支持范围内；
    - config 全量覆盖：密码字段若传 '******'（列表里的掩码）则沿用原已加密值，
      否则按明文重新 AES 加密；
    - 保存前重新连接测试，失败则回 400 并给出具体错误（与创建一致）；
    - 更新成功后释放该数据源缓存的引擎连接池，迫使下次请求用新连接参数重建。
    """
    async with session_scope() as s:
        ds = (await s.execute(
            select(DataSource).where(DataSource.id == ds_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据源不存在")
        new_name = (req.name or ds.name).strip()
        new_type = req.type or ds.type
        if new_type not in ("mysql", "postgresql", "sqlserver"):
            raise HTTPException(status_code=400, detail="不支持的数据源类型")
        # 名称唯一（排除自身）
        if new_name.lower() != ds.name.lower():
            exists = (await s.execute(
                select(DataSource).where(
                    func.lower(DataSource.name) == new_name.lower(),
                    DataSource.id != ds_id))).first()
            if exists:
                raise HTTPException(status_code=400, detail="数据源名称已存在")

        # 合并 config：库里存的是密文，先还原成明文，再统一做单层加密，
        # 避免「编辑时沿用原密码」导致对密文二次加密（连接必然失败）。
        stored_cfg = dict(ds.config or {})
        for k in _PWD_FIELDS:
            v = stored_cfg.get(k)
            if v:
                try:
                    stored_cfg[k] = decrypt_secret(v)
                except Exception:
                    pass  # 已是明文（兼容历史数据）则保持原样
        merged_cfg = copy.deepcopy(stored_cfg)
        if req.config:
            merged_cfg.update(req.config)
        # 密码掩码还原：前端回传 '******' 表示保持原密码（此时 stored_cfg 已是明文）
        for k in _PWD_FIELDS:
            if merged_cfg.get(k) == "******":
                merged_cfg[k] = stored_cfg.get(k, "")
        enc_cfg = _encrypt_config(merged_cfg)

        # 重新连接测试（适配器内部会自行解密密码）
        cls = get_adapter_class(new_type)
        import uuid as _uuid
        test_id = f"__test_{_uuid.uuid4().hex}"
        adapter = cls(test_id, new_type, enc_cfg)
        ok, err = await adapter.test_connection()
        await dispose_engines(test_id)
        if not ok:
            raise HTTPException(status_code=400, detail=f"连接测试失败：{err}")

        ds.name = new_name
        ds.type = new_type
        ds.config = enc_cfg
        ds.status = "ok"
        await s.flush()
    # 释放真实 source_id 的缓存引擎，避免用旧连接参数继续服务（H-3）
    await dispose_engines(ds_id)
    await bump_version()
    return {"id": ds.id, "name": ds.name, "type": ds.type}


@secure_router.get("/datasources/{ds_id}/schema")
async def schema(ds_id: str, q: str = "", schema: str = "",
                 page: int = 1, size: int = 50):
    async with session_scope() as s:
        ds = (await s.execute(select(DataSource).where(DataSource.id == ds_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据源不存在")
    adapter = get_adapter_class(ds.type)(ds.id, ds.type, ds.config)
    return await adapter.list_tables(q=q, schema=schema, page=page, size=size)


async def _get_adapter(ds_id: str):
    """加载数据源并构造对应适配器（Schema 浏览器复用）。"""
    async with session_scope() as s:
        ds = (await s.execute(select(DataSource).where(DataSource.id == ds_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据源不存在")
    return ds, get_adapter_class(ds.type)(ds.id, ds.type, ds.config)


@secure_router.get("/datasources/{ds_id}/schemas")
async def list_schemas(ds_id: str):
    """列出该数据源的 schema（库/模式/架构），供 Schema 浏览器切换命名空间。"""
    _, adapter = await _get_adapter(ds_id)
    try:
        return await adapter.list_schemas()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取 schema 失败：{e}")


@secure_router.get("/datasources/{ds_id}/tables/{table}/columns")
async def table_columns(ds_id: str, table: str, schema: str = ""):
    """反射单表字段（列名/类型/可空/默认值/主键），供点选拼 SQL。"""
    _, adapter = await _get_adapter(ds_id)
    try:
        return await adapter.list_columns(table, schema)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取字段失败：{e}")


class QueryPreviewReq(BaseModel):
    sql: str
    limit: int = 50
    params: dict = {}
    explain: bool = False


@secure_router.post("/datasources/{ds_id}/query-preview")
async def query_preview(ds_id: str, req: QueryPreviewReq):
    """只读查询预览：管理员在 Schema 浏览器里拼出的 SQL 实时试跑（仅 SELECT，行数封顶）。

    支持命名参数绑定（:p1 / :p2 ...）与 explain 执行计划返回（A②/A⑤ 查询构建器复用）。
    """
    _, adapter = await _get_adapter(ds_id)
    try:
        return await adapter.preview_sql(req.sql, req.limit, req.params, req.explain)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"预览执行失败：{e}")


@secure_router.get("/datasources/{ds_id}/tables/{table}/foreign-keys")
async def table_foreign_keys(ds_id: str, table: str, schema: str = ""):
    """反射单表外键，供查询构建器的 JOIN 自动关联建议（A①）。"""
    _, adapter = await _get_adapter(ds_id)
    try:
        return await adapter.list_foreign_keys(table, schema)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取外键失败：{e}")


@secure_router.get("/apis")
async def list_apis(source_id: str = "", kind: str = "", published: Optional[str] = "",
                    tag: str = ""):
    async with session_scope() as s:
        # 用 LEFT OUTER JOIN：即使接口指向的数据源已被删除（source_id 失效），
        # 接口本身仍要在管理列表里出现，否则会出现「概览有 N 个、管理里一个都看不到」的不一致。
        stmt = select(ApiDefinition, DataSource.type, DataSource.name).outerjoin(
            DataSource, DataSource.id == ApiDefinition.source_id)
        if source_id:
            stmt = stmt.where(ApiDefinition.source_id == source_id)
        if kind:
            stmt = stmt.where(ApiDefinition.kind == kind)
        if tag:
            stmt = stmt.where(ApiDefinition.tag == tag)
        if published in ("true", "false"):
            stmt = stmt.where(ApiDefinition.published == (published == "true"))
        rows = (await s.execute(stmt)).all()
        return [{"id": r[0].id, "name": r[0].name, "kind": r[0].kind,
                 "resource": r[0].resource, "base_path": r[0].base_path,
                 "methods": r[0].methods, "published": r[0].published,
                 "remark": r[0].remark, "tag": r[0].tag or "",
                 "source_id": r[0].source_id, "source_type": r[1] or "",
                 "source_name": r[2] or ""}
                for r in rows]


@secure_router.get("/apis/{api_id}")
async def get_api(api_id: str):
    async with session_scope() as s:
        a = (await s.execute(select(ApiDefinition).where(ApiDefinition.id == api_id))).scalar_one_or_none()
        if not a:
            raise HTTPException(status_code=404, detail="接口不存在")
        return {"id": a.id, "name": a.name, "kind": a.kind, "resource": a.resource,
                "base_path": a.base_path, "methods": a.methods, "sql": a.sql,
                "params": a.params, "remark": a.remark,
                "tag": a.tag or "", "published": a.published, "source_id": a.source_id}


class ApiCreate(BaseModel):
    source_id: str
    name: str
    base_path: str                  # 对外路径段（小写，无空格）
    methods: str = "GET"            # 逗号分隔：GET,POST,PUT,DELETE
    sql: Optional[str] = None       # 参数化 SQL 模板（含 :param）
    params: Optional[Union[dict, list]] = None   # 入参声明 {name: type} 或 [{name,type,...}]
    remark: Optional[str] = None    # 接口用途备注
    tag: Optional[str] = None       # 分组/标签（用于列表筛选）


@secure_router.post("/apis")
async def create_api(req: ApiCreate):
    async with session_scope() as s:
        ds = (await s.execute(
            select(DataSource).where(DataSource.id == req.source_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据源不存在")
        # 同一数据源内 base_path 必须唯一（REGISTRY key = (source_name, base_path)，重复会互相覆盖）
        dup = (await s.execute(
            select(ApiDefinition).where(
                ApiDefinition.source_id == req.source_id,
                func.lower(ApiDefinition.base_path) ==
                (req.base_path or "").strip().lstrip("/").lower())
        )).scalar_one_or_none()
        if dup:
            raise HTTPException(status_code=400,
                                detail="该数据源下已存在相同路径段（base_path）的接口")
        if not req.sql or not req.sql.strip():
            raise HTTPException(status_code=400, detail="接口需提供 sql")
        # 单语句 / 禁 DDL 校验（与运行时一致，避坑 #29）
        try:
            get_adapter_class(ds.type)._validate_sql(req.sql)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # 方法白名单 + 非空校验（服务端兜底，避免存进空 methods 导致分发异常）
        methods = [m.strip().upper() for m in (req.methods or "").split(",") if m.strip()]
        _ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE"}
        if not methods:
            raise HTTPException(status_code=400, detail="methods 不能为空")
        if any(m not in _ALLOWED_METHODS for m in methods):
            raise HTTPException(status_code=400, detail="methods 仅支持 GET,POST,PUT,DELETE")
        # API Hub SQL 生成规范校验（方言感知：INSERT/UPDATE/DELETE 仅 POST、
        # 占位符 :xxx、NULL 语法、高危 DML 的 WHERE + 行数兜底、SQL Server TOP 括号等）
        _vr = validate_api_sql(req.sql, ds.type, ",".join(methods))
        if _vr["errors"]:
            raise HTTPException(status_code=400, detail="；".join(_vr["errors"]))
        if not (req.base_path and req.base_path.strip()):
            raise HTTPException(status_code=400, detail="接口需指定 base_path")
        api = ApiDefinition(
            source_id=req.source_id,
            name=req.name,
            kind="custom",
            resource=None,
            base_path=(req.base_path or "").strip().lstrip("/"),
            methods=",".join(methods),
            remark=req.remark or "",
            tag=((req.tag or "").strip() or None),
            sql=req.sql,
            params=req.params or {},
        )
        s.add(api)
        await s.flush()
        return {"id": api.id, "name": api.name, "kind": api.kind,
                "remark": api.remark}


class ApiUpdate(BaseModel):
    name: Optional[str] = None
    methods: Optional[str] = None
    sql: Optional[str] = None
    params: Optional[Union[dict, list]] = None
    base_path: Optional[str] = None
    remark: Optional[str] = None
    tag: Optional[str] = None


@secure_router.put("/apis/{api_id}")
async def update_api(api_id: str, req: ApiUpdate):
    async with session_scope() as s:
        a = (await s.execute(select(ApiDefinition).where(ApiDefinition.id == api_id))).scalar_one_or_none()
        if not a:
            raise HTTPException(status_code=404, detail="接口不存在")
        # 改了就重新服务端校验（与运行时一致，避免存进非法 SQL/方法）
        if req.methods is not None:
            methods = [m.strip().upper() for m in req.methods.split(",") if m.strip()]
            _ALLOWED = {"GET", "POST", "PUT", "DELETE"}
            if not methods:
                raise HTTPException(status_code=400, detail="methods 不能为空")
            if any(m not in _ALLOWED for m in methods):
                raise HTTPException(status_code=400, detail="methods 仅支持 GET,POST,PUT,DELETE")
            req.methods = ",".join(methods)
        if req.sql is not None and req.sql.strip():
            # 用接口所属数据源的真实类型选择适配器校验（H-18：原硬编码 RelationalAdapter
            # 会忽略非关系型/其他类型数据源的校验规则差异）
            ds = (await s.execute(
                select(DataSource).where(DataSource.id == a.source_id))).scalar_one_or_none()
            adapter_cls = get_adapter_class(ds.type) if ds else None
            if adapter_cls is None:
                raise HTTPException(status_code=400, detail="接口关联的数据源不存在或类型不支持")
            try:
                adapter_cls._validate_sql(req.sql)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            # API Hub SQL 生成规范校验（与创建一致；方法取「本次提交 or 原值」）
            _eff_methods = req.methods if req.methods is not None else a.methods
            _vr = validate_api_sql(req.sql, ds.type, _eff_methods or "")
            if _vr["errors"]:
                raise HTTPException(status_code=400, detail="；".join(_vr["errors"]))
        if req.base_path is not None:
            bp = req.base_path.strip().lstrip("/")
            if not bp:
                raise HTTPException(status_code=400, detail="custom 接口 base_path 不能为空")
            # 改名路径段时，避免与同数据源下其他接口冲突（否则 REGISTRY 互相覆盖）
            dup = (await s.execute(
                select(ApiDefinition).where(
                    ApiDefinition.source_id == a.source_id,
                    func.lower(ApiDefinition.base_path) == bp.lower(),
                    ApiDefinition.id != api_id)
            )).scalar_one_or_none()
            if dup:
                raise HTTPException(status_code=400,
                                    detail="该数据源下已存在相同路径段（base_path）的接口")
        # 路由相关字段变更前快照（仅这些字段变化才需要重载 REGISTRY）
        _route_fields = ("methods", "sql", "base_path", "source_id", "params")
        _before = {f: getattr(a, f) for f in _route_fields}
        for f in ("name", "methods", "sql", "params", "base_path", "remark"):
            v = getattr(req, f)
            if v is not None:
                setattr(a, f, v.strip().lstrip("/") if f == "base_path" else v)
        if req.tag is not None:
            a.tag = (req.tag.strip() or None)
        await s.flush()
        _after = {f: getattr(a, f) for f in _route_fields}
        _route_changed = any(_before[f] != _after[f] for f in _route_fields)
        _was_published = a.published
    # 仅当「已发布」且「路由相关字段」变化时才重载 REGISTRY（改 tag/remark 或未发布接口无需重载）
    if _was_published and _route_changed:
        await bump_version()
    return {"ok": True}


@secure_router.post("/apis/{api_id}/publish")
async def publish(api_id: str):
    async with session_scope() as s:
        a = (await s.execute(select(ApiDefinition).where(ApiDefinition.id == api_id))).scalar_one_or_none()
        if not a:
            raise HTTPException(status_code=404, detail="接口不存在")
        # 关联数据源不存在（如被删除）→ 拒绝发布，避免「已发布却永远 404」的孤儿接口
        ds = (await s.execute(
            select(DataSource).where(DataSource.id == a.source_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=400,
                                detail="该接口关联的数据源不存在（可能已被删除），无法发布；"
                                       "请先重建数据源或在编辑中重新选择数据源后再发布。")
        a.published = True
    await bump_version()
    return {"ok": True}


@secure_router.post("/apis/{api_id}/unpublish")
async def unpublish(api_id: str):
    async with session_scope() as s:
        a = (await s.execute(select(ApiDefinition).where(ApiDefinition.id == api_id))).scalar_one_or_none()
        if not a:
            raise HTTPException(status_code=404, detail="接口不存在")
        a.published = False
    await bump_version()
    return {"ok": True}


@secure_router.delete("/apis/{api_id}")
async def delete_api(api_id: str):
    async with session_scope() as s:
        a = (await s.execute(select(ApiDefinition).where(ApiDefinition.id == api_id))).scalar_one_or_none()
        if not a:
            raise HTTPException(status_code=404, detail="接口不存在")
        await s.delete(a)
    await bump_version()
    return {"ok": True}




@secure_router.get("/stats/overview")
async def overview():
    return await get_overview()


@secure_router.get("/stats/realtime")
async def realtime(minutes: int = 60):
    """实时指标：最近 minutes 分钟的逐分钟序列 + 窗口 KPI（含 p95/p99 与缓存命中率）。"""
    return await get_realtime(minutes)


@secure_router.get("/stats/{api_id}/trend")
async def trend(api_id: str, days: int = 30):
    return await get_trend(api_id, days)


def _iso_utc(dt) -> str:
    """把（naive UTC）时间戳序列化为带 Z 的 ISO 字符串，便于前端按本地时区展示。

    log_date 统一存 UTC；前端用 new Date(iso).toLocaleString() 转成浏览器本地时间，
    避免 SQLite(CURRENT_TIMESTAMP=UTC) 与 MySQL(NOW=服务器时区) 不一致导致的展示错位。
    """
    if not dt:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(dt)


@secure_router.get("/logs")
async def logs(api_id: str = "", source_id: str = "", status: int = 0,
               caller_ip: str = "", page: int = 1, size: int = 20):
    async with session_scope() as s:
        # 数据源筛选：兼容新数据(存 UUID)与历史数据(存的是数据源名称)
        src_match: set = set()
        if source_id:
            ds = (await s.execute(
                select(DataSource).where(DataSource.id == source_id))).scalar_one_or_none()
            src_match.add(source_id)
            if ds:
                src_match.add(ds.name)
        stmt = select(ApiLog)
        if api_id:
            stmt = stmt.where(ApiLog.api_id == api_id)
        if src_match:
            stmt = stmt.where(ApiLog.source_id.in_(src_match))
        if status:
            stmt = stmt.where(ApiLog.status_code == status)
        if caller_ip:
            stmt = stmt.where(ApiLog.caller_ip == caller_ip)
        total = (await s.execute(
            select(func.count()).select_from(stmt.subquery()))).scalar() or 0
        rows = (await s.execute(
            stmt.order_by(ApiLog.log_date.desc(), ApiLog.id.desc())
            .limit(size).offset((page - 1) * size))).scalars().all()
        # 数据源名称映射（兼容 UUID 与历史名称两种存储形态）
        all_src = (await s.execute(select(DataSource.id, DataSource.name))).all()
        id_to_name = {sid: name for sid, name in all_src}
        name_set = {nm for _, nm in all_src}
        def _name(n):
            if not n:
                return ""
            if n in id_to_name:
                return id_to_name[n]
            return n if n in name_set else ""
        return {"items": [{
            "id": r.id, "method": r.method, "path": r.path,
            "status_code": r.status_code, "latency_ms": r.latency_ms,
            "caller_ip": r.caller_ip, "created_at": _iso_utc(r.log_date),
            "error_type": r.error_type,
            "source_id": r.source_id,
            "source_name": _name(r.source_id),
            "api_id": r.api_id,
        } for r in rows], "total": total, "page": page, "size": size}


@secure_router.get("/logs/{log_id}")
async def log_detail(log_id: str):
    async with session_scope() as s:
        r = (await s.execute(select(ApiLog).where(ApiLog.id == log_id))).scalar_one_or_none()
        if not r:
            raise HTTPException(status_code=404, detail="日志不存在")
        return {"id": r.id, "method": r.method, "path": r.path,
                "query_params": r.query_params, "status_code": r.status_code,
                "latency_ms": r.latency_ms, "caller_ip": r.caller_ip,
                "user_agent": r.user_agent, "error_type": r.error_type,
                "error_detail": r.error_detail, "created_at": _iso_utc(r.log_date)}


class PasswordReq(BaseModel):
    current: str
    new: str
    confirm: str


@secure_router.post("/account/password")
async def change_password(req: PasswordReq):
    if not await verify_password(req.current):
        raise HTTPException(status_code=400, detail="当前口令错误")
    if len(req.new) < 6 or req.new != req.confirm:
        raise HTTPException(status_code=400, detail="新口令不合法或两次不一致")
    from .auth import _hash
    from .models import AdminAccount
    async with session_scope() as s:
        acc = (await s.execute(select(AdminAccount).limit(1))).scalar_one_or_none()
        acc.password_hash = _hash(req.new)
        acc.pwd_changed_at = datetime.utcnow()  # 改密即让旧令牌失效（pc 不匹配）
    # 失效 verify_token 的 pwd_changed_at 短缓存，使旧令牌立即被拒（不等 5s TTL 自然过期）
    await cache.delete("auth:pwd_changed_at")
    return {"ok": True}


@secure_router.get("/settings")
async def get_settings():
    return _runtime_settings


@secure_router.get("/openapi")
async def get_openapi():
    """已发布自定义接口的 OpenAPI 3.0 文档（JSON）。"""
    from .openapi_spec import build_openapi
    return await build_openapi()


@secure_router.get("/apis/{api_id}/openapi")
async def get_api_openapi(api_id: str):
    """单个自定义接口的 OpenAPI 3.0 文档（不限是否发布，便于预览）。"""
    from .openapi_spec import build_openapi_for
    spec = await build_openapi_for(api_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="接口不存在或非自定义接口")
    return spec


class SuggestParamsReq(BaseModel):
    sql: str                        # 命名参数化 SQL 模板（含 :param）


@secure_router.post("/apis/suggest-params")
async def suggest_params(req: SuggestParamsReq):
    """根据 SQL 里的 :bind 命名参数，推断一份入参声明草稿。

    直接复用运行时的 _bind_names（已剔除字符串字面量，避免把 'xx' 里的
    :foo 误判为绑定参数）。这样「推断」与「运行时强制校验」使用同一套解析，
    保证生成的声明与 SQL 真实用到的参数完全一致——从根上避免漏写 id / 漏写 [ ]。
    """
    from .business import _bind_names
    names = _bind_names(req.sql or "")
    params = []
    for n in names:
        lower = n.lower()
        # id 类（id / user_id / order_id ...）推断为 int，其余默认 string
        typ = "int" if (lower == "id" or lower.endswith("id")) else "string"
        params.append({"name": n, "type": typ,
                       "required": True, "in": "query", "desc": ""})
    return {"params": params, "count": len(params)}


async def load_runtime_settings():
    """启动时从库里读回整份设置（持久化）。库里没有则用当前 env 种子值。"""
    try:
        async with session_scope() as s:
            row = (await s.execute(
                select(SystemSetting).where(SystemSetting.key == _RUNTIME_KEY)
            )).scalar_one_or_none()
            if row and row.value:
                saved = json.loads(row.value)
                _runtime_settings.update({k: v for k, v in saved.items()
                                         if k in _runtime_settings})
    except Exception:
        # 读库失败（如首启建表未完成）不影响启动，沿用内存默认值
        pass


class SettingsUpdate(BaseModel):
    cors_enabled: Optional[bool] = None
    cors_allow_origins: Optional[list] = None
    cors_allow_credentials: Optional[bool] = None
    sample_qps_threshold: Optional[int] = None
    sample_ratio: Optional[float] = None
    slow_ms: Optional[int] = None
    log_retention_days: Optional[int] = None


@secure_router.put("/settings")
async def put_settings(req: SettingsUpdate):
    changed = {}
    for f in ("cors_enabled", "cors_allow_origins", "cors_allow_credentials",
              "sample_qps_threshold", "sample_ratio", "slow_ms", "log_retention_days"):
        v = getattr(req, f)
        if v is not None:
            if _runtime_settings.get(f) != v:
                changed[f] = v
            _runtime_settings[f] = v
    # 持久化整份设置，重启后不丢
    try:
        async with session_scope() as s:
            await s.merge(SystemSetting(key=_RUNTIME_KEY,
                                value=json.dumps(_runtime_settings, ensure_ascii=False)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存设置失败：{e}")
    # 「日志保留天数」变更 → 立即清理一次，不用等周期任务（最长原本要等 1 小时）
    if "log_retention_days" in changed:
        try:
            from .stats import maintain_logs
            await maintain_logs()
        except Exception:
            pass  # 清理失败不影响设置已保存的结果
    return _runtime_settings
