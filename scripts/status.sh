#!/bin/bash
# Quick status check for the trading bot
# Usage: bash scripts/status.sh
set -e

cd "$(dirname "$0")/.."

echo "=== Container Status ==="
docker compose ps

echo ""
echo "=== Last 30 log lines ==="
docker compose logs --tail=30 --no-log-prefix

echo ""
echo "=== Resource Usage ==="
docker stats --no-stream polymarket-unified 2>/dev/null || echo "Container not running"
