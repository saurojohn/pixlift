#!/usr/bin/env bash
# PixLift 停止脚本
# - 优先用 PGID（= PID，Python uvicorn 自己是 PGID leader）杀整组
# - PGID 不存在时 fallback 到 PID 单独杀 wrapper
# - Ctrl+C 安全退出

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$PROJECT_DIR/pixlift.pid"
PGID_FILE="$PROJECT_DIR/.pixlift.pgid"

# Ctrl+C 时强杀并清掉 pid file，避免半停止状态
trap 'kill -9 "$PID" 2>/dev/null || true; rm -f "$PID_FILE" "$PGID_FILE"; exit 130' INT TERM

if [ ! -f "$PID_FILE" ]; then
    echo "[pixlift] not running (no pid file)"
    exit 0
fi

PID="$(cat "$PID_FILE")"

if ! kill -0 "$PID" 2>/dev/null; then
    echo "[pixlift] process $PID not alive, removing stale pid file"
    rm -f "$PID_FILE" "$PGID_FILE"
    exit 0
fi

# 取 PGID（macOS/Linux 通用：用 ps）
PGID="$(ps -o pgid= -p "$PID" 2>/dev/null | tr -d ' ' || true)"

# 杀整组（PGID 即 PID，因为 uvicorn 是 PGID leader）
TARGET="${PGID:-$PID}"
echo "[pixlift] stopping pgid=$TARGET (pid=$PID)"
kill -TERM -"$TARGET" 2>/dev/null || kill -TERM "$PID" 2>/dev/null || true

# 等最多 10 秒
for ((i = 0; i < 20; i++)); do
    if ! kill -0 "$PID" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

if kill -0 "$PID" 2>/dev/null; then
    echo "[pixlift] still alive after 10s, force killing (pgid=$TARGET)"
    kill -9 -"$TARGET" 2>/dev/null || kill -9 "$PID" 2>/dev/null || true
fi

echo "[pixlift] stopped (pid=$PID)"
rm -f "$PID_FILE" "$PGID_FILE"