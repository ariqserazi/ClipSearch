#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

docker compose build drama-clip-scout
docker compose up -d --no-deps --force-recreate drama-clip-scout

echo "Updated only drama-clip-scout. Hermes and local data were not touched."
