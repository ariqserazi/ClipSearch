#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

docker compose stop drama-clip-scout || true
docker rm drama-clip-scout 2>/dev/null || true

echo "Removed only the drama-clip-scout container."
echo "SQLite data in ./data was not deleted."
echo "Hermes, ~/.hermes, and the hermes container were not touched."
echo
echo "Destructive data reset, if you truly want it later:"
echo "rm -f \"$PROJECT_DIR/data/clips.db\" \"$PROJECT_DIR/data/clips.db-shm\" \"$PROJECT_DIR/data/clips.db-wal\""
