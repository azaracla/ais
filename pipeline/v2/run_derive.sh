#!/bin/bash
set -euo pipefail

# Nettoyer les fichiers temporaires
rm -f /tmp/derive_*.sql

# Load environment variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../.env"

LOCAL_CATALOG="/tmp/ais.ducklake"

# Parameters: DATE (required), HOUR (required, 00-23)
DATE=${1:-"$(date -d "yesterday" +%Y-%m-%d)"}
HOUR=${2:-"00"}

echo "🏗️ Deriving gold tables for date: ${DATE} hour: ${HOUR}"

# Pad hour to 2 digits
HOUR_PADDED=$(printf "%02d" "$((10#$HOUR))" 2>/dev/null || echo "00")

# Create final SQL file
TMP_SQL=$(mktemp /tmp/derive_final_XXXXXX.sql)
trap "rm -f $TMP_SQL" EXIT

# Write header
cat > "${TMP_SQL}" <<EOF
INSTALL httpfs;
LOAD httpfs;
INSTALL ducklake;
LOAD ducklake;

SET s3_endpoint='${OVH_ENDPOINT//https:\/\/}';
SET s3_access_key_id='${OVH_ACCESS_KEY}';
SET s3_secret_access_key='${OVH_SECRET_KEY}';
SET s3_region='${OVH_REGION}';
SET s3_url_style='path';
SET s3_use_ssl=true;

ATTACH '${LOCAL_CATALOG}' AS ais_lake (
    TYPE ducklake,
    DATA_PATH 's3://${BUCKET_PUBLIC}/v2/ais.ducklake.files/',
    OVERRIDE_DATA_PATH true
);

EOF

# Append derive_tables.sql with date and hour parameters replaced
sed "s/:target_date/'${DATE}'/g; s/:target_hour/${HOUR_PADDED}/g" "${SCRIPT_DIR}/sql/derive_tables.sql" >> "${TMP_SQL}"

# Execute
duckdb -f "${TMP_SQL}"

echo "✅ Dérivation terminée pour ${DATE} ${HOUR}:00"

# Auto-save catalog to S3
"${SCRIPT_DIR}/save_catalog.sh" > /dev/null 2>&1 || true
