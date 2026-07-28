#!/usr/bin/env bash
# 重新预置离线 Python wheels（升级依赖版本时用）。
# 在「能联网」的机器上运行，把 wheels 下载到 pywheels/{amd64,arm64}。
# 下载逻辑与 Dockerfile 离线安装保持一致：
#   - 目标 Python 3.13（cp313），manylinux_2_28 优先、退回 manylinux_2_17；
#   - 仅下载二进制 wheel（--only-binary=:all:），不拉源码包，保证容器内零编译。
# 用法： bash fetch-pywheels.sh
set -e
PIP="${PIP:-pip}"
REQ_FILE="${REQ_FILE:-requirements.txt}"
if [ ! -f "$REQ_FILE" ]; then echo "FATAL: $REQ_FILE not found (run from project root)"; exit 1; fi

for arch in amd64 arm64; do
  dest="pywheels/$arch"
  mkdir -p "$dest"
  plat28="manylinux_2_28_${arch}"
  plat17="manylinux_2_17_${arch}"
  echo "===== $arch ====="
  while IFS= read -r line; do
    spec="$(echo "$line" | sed -E 's/#.*//; s/^[[:space:]]+//; s/[[:space:]]+$//')"
    [ -z "$spec" ] && continue
    name="$(echo "$spec" | sed -E 's/\[.*//; s/[<>=!~].*//')"
    ok=0
    for plat in "$plat28" "$plat17"; do
      if "$PIP" download -q -i https://pypi.org/simple --no-cache-dir "$spec" \
          --python-version 313 --platform "$plat" --abi cp313 --only-binary=:all: -d "$dest" >/dev/null 2>&1; then
        echo "OK   $name ($plat)"; ok=1; break
      fi
    done
    [ $ok -eq 0 ] && echo "FAIL $name (no cp313 wheel for $arch)"
  done < "$REQ_FILE"
  # uvloop 单独拉取（不在 requirements.txt 内）
  for plat in "$plat28" "$plat17"; do
    if "$PIP" download -q -i https://pypi.org/simple --no-cache-dir uvloop \
        --python-version 313 --platform "$plat" --abi cp313 --only-binary=:all: -d "$dest" >/dev/null 2>&1; then
      echo "OK   uvloop ($plat)"; break
    fi
  done
done
echo "done -> pywheels/{amd64,arm64}"
