#!/bin/bash
set -euo pipefail

# Load environment variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../.env"

# Parameters: DATE (required)
DATE=${1:-"$(date -d "yesterday" +%Y-%m-%d)"}

echo "🚀 Processing full day: ${DATE}"
echo "======================================================"

# Process each hour using full pipeline
for HOUR in $(seq -f "%02g" 0 23); do
    echo ""
    echo "🕒 Processing hour ${HOUR}:00..."
    "${SCRIPT_DIR}/run_full_pipeline.sh" "${DATE}" "${HOUR}"
    echo "✅ Hour ${HOUR}:00 completed"
done

echo ""
echo "======================================================"
echo "✅ Full day ${DATE} processing complete"
