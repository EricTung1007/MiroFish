#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Starting MiroFish..."
"$ROOT_DIR/start-local.sh"

echo
echo "Opening http://127.0.0.1:3000 ..."
open "http://127.0.0.1:3000" >/dev/null 2>&1 || true

echo
echo "MiroFish is running."
echo "You can close this Terminal window. Use Stop MiroFish.command to stop the servers."
echo
read -r -p "Press Return to close this window..."
