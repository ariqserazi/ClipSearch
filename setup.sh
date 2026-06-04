#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "Creating Docker network drama-net if needed..."
docker network inspect drama-net >/dev/null 2>&1 || docker network create drama-net >/dev/null

if docker ps -a --format '{{.Names}}' | grep -Fxq 'hermes'; then
  if docker inspect -f '{{json .NetworkSettings.Networks}}' hermes | grep -q '"drama-net"'; then
    echo "Hermes container is already connected to drama-net."
  else
    echo "Connecting existing hermes container to drama-net..."
    docker network connect drama-net hermes
  fi
else
  echo "No Docker container named hermes was found. Skipping Hermes network connection."
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example."
else
  echo ".env already exists. Leaving it unchanged."
fi

mkdir -p data

echo
echo "Add your Reddit and X API credentials manually to:"
echo "$PROJECT_DIR/.env"
echo
echo "Safety note: this setup does not delete ~/.hermes and does not replace Hermes."
