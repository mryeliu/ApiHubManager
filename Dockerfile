# 基础镜像固定到多架构清单摘要（2026-07 的 python:3.13-slim）。
# 避免 floating tag 漂移导致构建机反复重新拉取基础层——曾因 Docker Hub CDN
# 拉取超时（TLS handshake timeout）而构建失败。下方 daemon 镜像加速可根治拉取问题。
FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

WORKDIR /app

# ===== 把整个构建上下文拷入（含离线预置的 odbc-debs/ 与 pywheels/）=====
COPY . .

# ===== ODBC Driver 18 for SQL Server（SQL Server 数据源依赖）=====
# 健壮性优先：构建上下文已预置 odbc-debs/msodbcsql18_<arch>.deb（离线包），
# 优先用 dpkg 离线安装，完全不访问微软 apt 仓库 —— 规避构建机访问
# packages.microsoft.com 不稳定 / GPG 校验失败导致的 exit 100。
# 未预置 .deb 时回退到微软官方 apt 仓库（带重试 + 密钥校验）。
ENV ACCEPT_EULA=Y
RUN set -eux; \
    apt-get update -o Acquire::Retries=3; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg unixodbc unixodbc-dev apt-transport-https gcc g++; \
    ARCH=$(dpkg --print-architecture); \
    if [ -f "odbc-debs/msodbcsql18_${ARCH}.deb" ]; then \
        echo "[ODBC] offline install from pre-staged deb (arch=${ARCH})"; \
        dpkg -i "odbc-debs/msodbcsql18_${ARCH}.deb" || apt-get install -f -y --no-install-recommends; \
    else \
        echo "[ODBC] no pre-staged deb -> Microsoft apt repo"; \
        for i in 1 2 3 4 5; do curl -fsSL -o /tmp/msft.asc https://packages.microsoft.com/keys/microsoft.asc && break; echo "retry $i"; sleep 5; done; \
        test -s /tmp/msft.asc || (echo "FATAL: Microsoft signing key download failed" >&2; exit 1); \
        gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg /tmp/msft.asc; \
        rm -f /tmp/msft.asc; \
        echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg arch=${ARCH}] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list; \
        apt-get update -o Acquire::Retries=3; \
        apt-get install -y --no-install-recommends msodbcsql18; \
    fi; \
    rm -rf odbc-debs; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    odbcinst -q -d | grep -q "ODBC Driver 18 for SQL Server" && echo "[ODBC] driver registered OK" || echo "[ODBC] WARN: driver not auto-listed; registers on first use"

# ===== Python 依赖：离线预置 wheels（pywheels/<arch>/），构建期零联网 =====
# 镜像内 glibc 2.36，manylinux_2_17 / manylinux_2_28 的 wheel 均可运行；
# 离线包已同时收入两种 tag，pip 安装时会自动挑选兼容版本。
RUN set -eux; \
    ARCH=$(dpkg --print-architecture); \
    if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; then W=/app/pywheels/arm64; else W=/app/pywheels/amd64; fi; \
    echo "[pip] offline install from $W"; \
    pip install --no-cache-dir --no-index --find-links=$W -r requirements.txt; \
    pip install --no-cache-dir --no-index --find-links=$W uvloop || true; \
    rm -rf /app/pywheels

# 多 worker 启动；生产管理后台前置 TLS（见避坑 #31）
# WORKERS 默认对齐 CPU 核数（nproc），单 worker 上限并发 1000，避免单进程被打爆
# 注意：--limit-concurrency 是 uvicorn 的参数，gunicorn 不支持，须用 --worker-connections
CMD ["sh", "-c", "gunicorn -k uvicorn.workers.UvicornWorker main:app -w ${WORKERS:-$(nproc)} --worker-connections 1000 -b 0.0.0.0:8000"]
