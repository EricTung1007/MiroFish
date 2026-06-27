#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

if lsof -ti tcp:3000 >/dev/null 2>&1 || lsof -ti tcp:5001 >/dev/null 2>&1; then
  echo "MiroFish appears to be running. Stopping it..."
  "$ROOT_DIR/stop-local.sh"
  echo
  echo "MiroFish stopped."
else
  echo "MiroFish appears to be stopped. Starting it..."
  "$ROOT_DIR/start-local.sh"
  echo
  echo "Opening http://127.0.0.1:3000 ..."
  open "http://127.0.0.1:3000" >/dev/null 2>&1 || true
  echo
  echo "MiroFish is running."
fi

echo
read -r -p "Press Return to close this window..."
