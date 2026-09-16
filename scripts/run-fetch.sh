#!/bin/bash
# Run the AI Newsletter fetcher

set -e

# Get the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Change to project root
cd "$PROJECT_ROOT"

# Create data and logs directories
mkdir -p data logs

# Timestamp for log file
TIMESTAMP=$(date '+%Y%m%d-%H%M%S')
LOG_FILE="logs/fetch-$TIMESTAMP.log"
LATEST_LOG="logs/latest.log"

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Run the fetcher
echo "🤖 Starting AI Newsletter fetch at $(date)"
echo "Logs: $LOG_FILE"

# Run with output to both console and log file
python3 scripts/fetch-news.py 2>&1 | tee "$LOG_FILE"

# Update symlink to latest log
ln -sf "$(basename "$LOG_FILE")" "$LATEST_LOG"

echo "✅ Completed at $(date)"