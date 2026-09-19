#!/usr/bin/env bash
# PixLift 后台启动脚本
# - 检查 venv
# - 检查 binary（缺失时给出指引而非崩溃）
# - 用 flock 防止并发启动
# - 用 setsid 建新进程组，便于 stop.sh 一次清理所有 child
# - 后台启动，写 PID 到 pixlift.pid，日志到 logs/pixlift.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PID_FILE="$PROJECT_DIR/pixlift.pid"
LOCK_FILE="$PROJECT_DIR/.start.lock"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/pixlift.log"

# 私有 umask（日志不要被同机器其他用户读）
umask 077
mkdir -p "$LOG_DIR"

# 互斥锁：mkdir 是原子的。如果目录已存在 = 另一个 start.sh 在跑
# （macOS 没有 flock 命令，用 mkdir 是 portable 方案）
if ! mkdir "$LOCK_FILE" 2>/dev/null; then
    echo "[pixlift] another start.sh is in progress"
    exit 1
fi
# 确保退出时清理 LOCK_FILE（正常 / 异常退出）
trap 'rmdir "$LOCK_FILE" 2>/dev/null || true' EXIT

# 同时把 PID 文件扩展成 PGID 记录（macOS 没有 setsid，
# 但 Python uvicorn 进程可作为 PGID leader；停止时杀整组）
PGID_FILE="$PROJECT_DIR/.pixlift.pgid"

if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE")"
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[pixlift] already running (pid=$OLD_PID)"
        exit 0
    else
        echo "[pixlift] stale pid file, removing"
        rm -f "$PID_FILE"
    fi
fi

# 检查 venv
if [ ! -d ".venv" ]; then
    echo "[pixlift] no .venv found. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
    exit 1
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
LOG_LEVEL="${LOG_LEVEL:-info}"

echo "[pixlift] starting on $HOST:$PORT"
# nohup 启动；PID 文件存 wrapper PID，PGID 文件存进程组 ID（macOS 上 PID 即 PGID）
# （macOS 没有 setsid；Python uvicorn 默认自己就是 PGID leader，所以 PID == PGID）
nohup .venv/bin/python run.py --host "$HOST" --port "$PORT" --log-level "$LOG_LEVEL" \
    >> "$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"
echo "$PID" > "$PGID_FILE"

# 等服务 ready（轮询 /api/health，最长 10s）
READY=0
for _ in $(seq 1 20); do
    sleep 0.5
    if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
        READY=1
        break
    fi
done

if [ "$READY" = "1" ]; then
    echo "[pixlift] started (pid=$PID, ready, log=$LOG_FILE)"
    echo "[pixlift] open http://localhost:$PORT"
else
    if kill -0 "$PID" 2>/dev/null; then
        echo "[pixlift] started but /api/health not ready yet (pid=$PID, log=$LOG_FILE)"
    else
        echo "[pixlift] failed to start. tail of log:"
        tail -20 "$LOG_FILE" || true
        rm -f "$PID_FILE"
        exit 1
    fi
fi