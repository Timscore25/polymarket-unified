#!/bin/bash
# Deploy/update polymarket-unified on VPS
# Usage: bash scripts/deploy.sh
set -e

cd "$(dirname "$0")/.."

echo "=== Pulling latest code ==="
git pull origin main

echo "=== Rebuilding container ==="
docker compose build --no-cache

echo "=== Restarting ==="
docker compose down
docker compose up -d

sleep 5

echo "=== Status ==="
docker compose ps
echo ""
echo "=== Recent logs ==="
docker compose logs --tail=20
