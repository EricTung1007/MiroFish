#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Stopping MiroFish..."
"$ROOT_DIR/stop-local.sh"

echo
echo "MiroFish stopped."
echo
read -r -p "Press Return to close this window..."
