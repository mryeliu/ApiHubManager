"""FastAPI 入口：挂载管理面 + 业务面 + 静态 UI，启动后台任务。

生产用 `gunicorn -k uvicorn.workers.UvicornWorker main:app -w N` 多 worker 启动。
管理面 /docs 默认关闭（生产保护，避坑 #24），需设 DOCS_ENABLED=1 开启。
"""
import asyncio
import gzip
import logging
import os
from contextlib import asynccontextmanager

# A3：Brotli 为可选依赖，安装则启用 br 压缩，否则仅 Gzip 兜底。
try:
    import brotli  # 可选依赖：用于 Brotli 压缩（A3）
    _BROTLI = True
except Exception:
    brotli = None
    _BROTLI = False

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.cache import cache
from app.metadata_db import init_db, dispose
from app.publisher import router as pub_router, reload as reload_registry, maybe_reload
from app.admin_routes import open_router, secure_router, load_runtime_settings
from app.cors import CorsRuntimeMiddleware
from app.log_writer import run as log_run
from app.stats import maintain_logs


async def _hot_reload_loop():
    while True:
        await asyncio.sleep(5)
        try:
            await maybe_reload()
        except Exception:
            logging.exception("热重载循环异常（已跳过本次，将持续重试）")


async def _partition_loop():
    while True:
        await asyncio.sleep(settings.LOG_MAINTENANCE_INTERVAL)
        try:
            await maintain_logs()
        except Exception:
            logging.exception("日志分区维护循环异常（已跳过本次，将持续重试）")


async def _wait_for_dependencies() -> None:
    """启动前等待 MySQL/Redis 真正就绪（带重试，兜底 docker-compose 竞态）。

    docker-compose 已用 depends_on: service_healthy 尽量延后 App 启动，但 MySQL
    官方镜像的 healthcheck（mysqladmin ping）在「数据库 / 用户尚未建好」时也可能
    返回 alive，导致 App 抢跑、连接失败、worker 启动崩溃。这里再做一层应用内重试：
    在 DB_STARTUP_WAIT_SECONDS（默认 120s）内反复重试 cache.connect() 与 init_db()，
    任一依赖未就绪都只告警并等待，不直接杀死启动；超时仍未就绪才真正报错，
    交给容器 restart 策略（unless-stopped）接管重启。
    """
    wait = int(os.getenv("DB_STARTUP_WAIT_SECONDS", "120"))
    backoff = 2.0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    last_err: BaseException | None = None
    while True:
        try:
            await cache.connect()
            await init_db()
            if last_err is not None:
                logging.info("MySQL/Redis 现已就绪，应用继续启动。")
            return
        except Exception as e:  # noqa: BLE001 启动期依赖未就绪属预期，需重试而非崩溃
            last_err = e
            remaining = deadline - loop.time()
            if remaining <= 0:
                logging.error(
                    "等待 MySQL/Redis 就绪超时（%ss），应用启动失败：%s: %s",
                    wait, type(e).__name__, e,
                )
                raise
            logging.warning(
                "MySQL/Redis 尚未就绪，%.0fs 后重试（剩 %.0fs）：%s: %s",
                backoff, remaining, type(e).__name__, e,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：等待依赖就绪（带重试，规避 docker-compose 竞态）
    await _wait_for_dependencies()
    # 生产安全自检：密钥仍为占位默认值则告警（令牌可被伪造 / 数据源密码可被解密）
    if (settings.JWT_SECRET == "CHANGE_ME_IN_PROD_JWT_SECRET"
            or settings.DB_SECRET_KEY == "CHANGE_ME_DB_SECRET_32BYTES_MIN"):
        logging.warning(
            "安全告警：JWT_SECRET / DB_SECRET_KEY 仍为占位默认值，生产环境必须设置强随机值"
            "（环境变量 JWT_SECRET / DB_SECRET_KEY），否则管理员令牌可被伪造、数据源密码可被解密。"
        )
    await load_runtime_settings()   # 读回持久化的系统设置（CORS 等）
    await reload_registry()
    app.state.tasks = [
        asyncio.create_task(log_run()),
        asyncio.create_task(_hot_reload_loop()),
        asyncio.create_task(_partition_loop()),
    ]
    yield
    # 关闭：先优雅等待后台任务结束，再释放数据库连接（M-1）
    for t in app.state.tasks:
        t.cancel()
    await asyncio.gather(*app.state.tasks, return_exceptions=True)
    await dispose()


docs_kwargs = (
    {} if settings.DOCS_ENABLED
    else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)

app = FastAPI(title="API 管理系统", lifespan=lifespan, **docs_kwargs)

# 开发辅助：后台 UI 与文档页不缓存，避免改了 HTML/JS 浏览器还用旧的
@app.middleware("http")
async def no_cache_admin(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/admin") or request.url.path.split("?")[0] in ("/docs", "/openapi.json", "/redoc"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# A3：响应压缩（Gzip/Brotli）。统一压缩 JSON 等文本响应，省带宽、降延迟。
# 仅对文本类响应、且体积超过阈值、且客户端声明支持时才压缩；已压缩/流式响应跳过。
@app.middleware("http")
async def compress_response(request: Request, call_next):
    response = await call_next(request)
    if not settings.COMPRESS_RESPONSES:
        return response
    if response.headers.get("Content-Encoding"):
        return response
    ct = response.headers.get("Content-Type", "")
    if not (ct.startswith("application/json")
            or ct.startswith("text/")
            or ct.startswith("application/javascript")
            or ct.startswith("application/xml")):
        return response
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)):
        return response  # 流式/文件响应不在此处压缩
    if len(body) < settings.COMPRESS_MIN_SIZE:
        return response
    accept = request.headers.get("Accept-Encoding", "")
    compressed = None
    encoding = None
    if _BROTLI and "br" in accept:
        compressed = brotli.compress(bytes(body))
        encoding = "br"
    elif "gzip" in accept:
        compressed = gzip.compress(bytes(body))
        encoding = "gzip"
    if not compressed or len(compressed) >= len(body):
        return response  # 压缩无收益则跳过
    response.body = compressed
    response.headers["Content-Encoding"] = encoding
    response.headers["Content-Length"] = str(len(compressed))
    response.headers["Vary"] = "Accept-Encoding"
    return response


# 业务面 CORS（浏览器侧）：运行时读取「系统设置」，即时生效（见 cors.py）
app.add_middleware(CorsRuntimeMiddleware)

app.include_router(open_router)
app.include_router(secure_router)
app.include_router(pub_router)

# 根路径 → 管理后台
@app.get("/")
async def root_redirect(request: Request):
    return RedirectResponse(url="/admin/", status_code=302)

# 网站图标：在静态挂载之前注册，优先于 /admin 静态目录命中。
# 源文件为项目根目录的 api_photo.ico，根路径与后台路径都会请求 favicon.ico。
_ICON_PATH = os.path.join(os.path.dirname(__file__), "api_photo.ico")

@app.get("/favicon.ico")
@app.get("/admin/favicon.ico")
async def favicon():
    return FileResponse(_ICON_PATH, media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})

# 管理后台静态 UI
app.mount("/admin", StaticFiles(directory="static", html=True), name="admin")


if __name__ == "__main__":
    import uvicorn
    # 直接传入 app 对象，避免 uvicorn 用字符串 "main:app" 再次 import 本模块
    # （调试器 / 多进程下二次导入会导致重复建 app、任务、引擎，甚至被信号误关）。
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, reload=False)
