#!/usr/bin/env bash
# Paste2Run 启动脚本（macOS / Linux）
set -e
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "没找到 Python 3，请先安装：https://www.python.org/downloads/"
  exit 1
fi
exec "$PY" p2r.py up "$@"
