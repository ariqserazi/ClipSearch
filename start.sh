#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example."
  echo "Reminder: add your Reddit and X API credentials to .env when you are ready."
  echo
fi

if ! docker network inspect drama-net >/dev/null 2>&1; then
  echo "Docker network drama-net does not exist yet."
  echo "Run ./setup.sh first so Drama Clip Scout can share a network with Hermes."
  exit 1
fi

docker compose up -d --build drama-clip-scout

echo
echo "Drama Clip Scout UI:"
echo "http://127.0.0.1:8787/ui"
echo
echo "Drama Clip Scout API:"
echo "http://127.0.0.1:8787"
echo
echo "Drama Clip Scout API Docs:"
echo "http://127.0.0.1:8787/docs"
echo
echo "Hermes Dashboard:"
echo "http://127.0.0.1:9119"
echo
echo "Hermes Gateway:"
echo "http://127.0.0.1:8642"
echo
echo "Internal Docker URL for Hermes to call:"
echo "http://drama-clip-scout:8787/agent/search-clips"
