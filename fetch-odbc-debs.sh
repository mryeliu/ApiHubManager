#!/usr/bin/env bash
# 预下载 Microsoft ODBC Driver 18 的 .deb 包，使 Docker 构建完全离线，
# 避开 packages.microsoft.com 仓库的网络/GPG 波动（典型症状：apt-get update exit 100）。
# 在 Docker 构建机上运行一次即可（需能访问 packages.microsoft.com）。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)/odbc-debs"
mkdir -p "$DIR"
BASE="https://packages.microsoft.com/debian/12/prod"
for arch in amd64 arm64; do
  echo "== $arch =="
  idx=$(curl -fsSL "$BASE/dists/bookworm/main/binary-$arch/Packages")
  fn=$(echo "$idx" | awk '/^Package: msodbcsql18$/{f=1;next} f&&/^Filename:/{print $2;exit}')
  echo "  下载 $fn"
  curl -fsSL -o "$DIR/msodbcsql18_${arch}.deb" "$BASE/$fn"
done
ls -l "$DIR"
echo "完成。现在可直接构建：docker compose up -d --build"
