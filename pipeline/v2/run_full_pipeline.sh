#!/bin/bash
set -euo pipefail

# Load environment variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../.env"

LOCAL_CATALOG="/tmp/ais.ducklake"

# Parameters: DATE (optional, default yesterday), HOUR (optional, default 00)
DATE=${1:-"$(date -d "yesterday" +%Y-%m-%d)"}
HOUR=${2:-00}

echo "🚀 Starting DuckLake v2 pipeline for date: ${DATE} hour: ${HOUR}"
echo "======================================================"
echo "Catalog: ${LOCAL_CATALOG}"
echo "Data: s3://${BUCKET_PUBLIC}/v2/ais.ducklake.files/"
echo ""

# 1. Initialize DuckLake (if needed - creates tables)
echo "🔧 Initializing DuckLake v2..."
"${SCRIPT_DIR}/run_init.sh"

# 2. Consolidation - read NDJSON.zst and insert into messages
echo ""
echo "📦 Consolidation pour ${DATE} ${HOUR}:00..."
"${SCRIPT_DIR}/run_consolidate.sh" "${DATE}" "${HOUR}"

# 3. Derive gold tables from messages
echo ""
echo "🏗️ Dérivation des tables gold pour ${DATE} ${HOUR}:00..."
"${SCRIPT_DIR}/run_derive.sh" "${DATE}" "${HOUR}"

# 4. Update vessels dimension table (once, not per hour)
echo ""
echo "🚢 Mise à jour de la table 'vessels'..."
"${SCRIPT_DIR}/run_vessels.sh"

echo ""
echo "======================================================"
echo "✅ Pipeline complet DuckLake v2 terminé pour ${DATE} ${HOUR}:00"
echo ""
echo "Le catalogue est en local: ${LOCAL_CATALOG}"
echo "Les données sont sur S3: s3://${BUCKET_PUBLIC}/v2/ais.ducklake.files/"
echo ""
echo "Pour sauvegarder sur S3: ./save_catalog.sh"
