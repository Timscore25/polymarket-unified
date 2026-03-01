#!/bin/bash
# Export full logs for P&L analysis
# Usage: bash scripts/export-logs.sh [output_dir]
set -e

cd "$(dirname "$0")/.."

OUTPUT_DIR="${1:-$HOME/log-exports}"
mkdir -p "$OUTPUT_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_FILE="$OUTPUT_DIR/trading_logs_$TIMESTAMP.jsonl"

echo "Exporting logs to $OUTPUT_FILE..."
docker compose logs --no-log-prefix --no-color > "$OUTPUT_FILE"

LINE_COUNT=$(wc -l < "$OUTPUT_FILE")
FILE_SIZE=$(du -h "$OUTPUT_FILE" | cut -f1)
echo "Exported $LINE_COUNT lines ($FILE_SIZE) to $OUTPUT_FILE"
