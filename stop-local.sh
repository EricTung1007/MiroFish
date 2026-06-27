#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$ROOT_DIR/.pids"

stop_pid_file() {
  local name="$1"
  local file="$PID_DIR/$name.pid"
  if [ ! -f "$file" ]; then
    echo "$name: no PID file"
    return
  fi

  local pid
  pid="$(cat "$file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "$name stopped: PID $pid"
  else
    echo "$name was not running"
  fi
  rm -f "$file"
}

stop_pid_file backend
stop_pid_file frontend

stop_port() {
  local port="$1"
  local pids
  pids="$(lsof -ti "tcp:$port" 2>/dev/null || true)"
  if [ -z "$pids" ]; then
    echo "port $port: no listener"
    return
  fi

  for pid in $pids; do
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "port $port stopped: PID $pid"
  done
}

stop_port 5001
stop_port 3000
