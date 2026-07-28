"""运行时 CORS 中间件。

读取 admin_routes._runtime_settings，使「系统设置」页的跨域配置**即时生效、且重启不丢**：
- cors_enabled=False → 不下发任何 CORS 头，浏览器拒绝一切跨域（默认安全；同源访问不受影响）
- cors_enabled=True  → 按 cors_allow_origins 放行：
    · 列表含 "0.0.0.0" 或 "*"        → 允许所有来源（回显请求 Origin）
    · 否则仅当请求 Origin 在列表中   → 回显该 Origin
    · 列表为空                          → 等于全部拒绝
- cors_allow_credentials=True → 附加 Allow-Credentials：此时来源**不建议**填 0.0.0.0/*，
  应写具体地址。注意：本实现回显的是具体 Origin（而非字面 *），因此「全放行 + 凭证」下
  浏览器实际会放行——代价是任何站点都能发起带凭证的跨域请求，存在安全隐患（前端已提示）。

用回显 Origin 而非 "*"，可兼容「带凭证」场景，避免 Starlette 通配+凭证冲突。
"""
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# 表示「允许所有来源」的特殊取值
_ALLOW_ALL = {"0.0.0.0", "*"}
# 全放行+凭证的安全告警只提示一次，避免每请求刷屏
_WARNED_ALLOW_ALL_CREDS = False


class CorsRuntimeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cfg = _get_cfg()
        origin = request.headers.get("origin")
        is_preflight = (
            request.method == "OPTIONS"
            and origin is not None
            and request.headers.get("access-control-request-method") is not None
        )

        # 关闭跨域：不下发任何 CORS 头 → 浏览器拦截跨域请求
        if not cfg.get("cors_enabled"):
            if is_preflight:
                return Response(status_code=403)
            return await call_next(request)

        # 已开启
        if is_preflight:
            resp = Response(status_code=204)
            self._apply(resp, request, cfg)
            if "access-control-allow-origin" not in resp.headers:
                return Response(status_code=403)
            return resp

        resp = await call_next(request)
        self._apply(resp, request, cfg)
        return resp

    @staticmethod
    def _apply(resp, request, cfg):
        origin = request.headers.get("origin")
        if not origin:
            return  # 同源请求无需 CORS 头
        allowed = cfg.get("cors_allow_origins") or []
        allow_all = bool(set(allowed) & _ALLOW_ALL)
        # 允许条件：填了 0.0.0.0/*（全部）或 来源在白名单内；否则拒绝（空列表=全部拒绝）
        if not (allow_all or origin in allowed):
            return
        # 放行：回显 Origin（带凭证也比 * 安全）
        resp.headers["access-control-allow-origin"] = origin
        resp.headers["access-control-allow-methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["access-control-allow-headers"] = "Content-Type, Authorization, X-Requested-With"
        # 安全约束：全放行（0.0.0.0/*）+ 凭证 = 任何站点都能发起带凭证的跨域请求（CSRF 风险）。
        # 此时强制忽略 Allow-Credentials；如需带凭证请改用具体来源白名单。
        allow_credentials = bool(cfg.get("cors_allow_credentials"))
        if allow_all and allow_credentials:
            global _WARNED_ALLOW_ALL_CREDS
            allow_credentials = False
            if not _WARNED_ALLOW_ALL_CREDS:
                _WARNED_ALLOW_ALL_CREDS = True
                logging.warning(
                    "CORS 配置为「允许所有来源」且开启凭证，已强制忽略 Allow-Credentials 以规避 CSRF 风险；"
                    "如需带凭证请改用具体来源白名单。"
                )
        if allow_credentials:
            resp.headers["access-control-allow-credentials"] = "true"
        resp.headers["access-control-max-age"] = "86400"
        resp.headers["vary"] = "Origin"


def _get_cfg():
    # 延迟导入，避免与 admin_routes / main 的循环依赖
    from .admin_routes import _runtime_settings
    return _runtime_settings
