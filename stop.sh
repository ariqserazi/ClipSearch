#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

docker compose stop drama-clip-scout

echo "Stopped drama-clip-scout. Hermes and local data were not touched."
