#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$ROOT_DIR/.pids"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$PID_DIR" "$LOG_DIR"

if [ -f "$PID_DIR/backend.pid" ] && kill -0 "$(cat "$PID_DIR/backend.pid")" 2>/dev/null; then
  echo "Backend already running: PID $(cat "$PID_DIR/backend.pid")"
else
  cd "$ROOT_DIR/backend"
  nohup "$ROOT_DIR/backend/.venv/bin/python" run.py > "$LOG_DIR/backend.log" 2>&1 &
  echo "$!" > "$PID_DIR/backend.pid"
  echo "Backend started: http://127.0.0.1:5001"
fi

if [ -f "$PID_DIR/frontend.pid" ] && kill -0 "$(cat "$PID_DIR/frontend.pid")" 2>/dev/null; then
  echo "Frontend already running: PID $(cat "$PID_DIR/frontend.pid")"
else
  cd "$ROOT_DIR/frontend"
  nohup npm run dev -- --host 127.0.0.1 > "$LOG_DIR/frontend.log" 2>&1 &
  echo "$!" > "$PID_DIR/frontend.pid"
  echo "Frontend started: http://127.0.0.1:3000"
fi

echo "Logs: $LOG_DIR"
echo "Stop with: $ROOT_DIR/stop-local.sh"
