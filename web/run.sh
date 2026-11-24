#!/bin/sh
# CodeBuddy 账号管理器 · 网页版启动脚本（Altare w1 沙盒）
# 数据目录 /root/cbam-data；崩溃自动重启；由沙盒开机钩子拉起。
cd "$(dirname "$0")" || exit 1
export APPDATA="${CBW_APPDATA:-/root/cbam-data}"
export TZ="${TZ:-Asia/Shanghai}"
export CBW_PORT="${CBW_PORT:-8080}"
PY="${CBW_PYTHON:-/root/cbam-venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
while true; do
  "$PY" server.py >> "$APPDATA/server.out" 2>&1
  echo "$(date '+%F %T') server exited ($?), restart in 5s" >> "$APPDATA/server.out"
  sleep 5
done
