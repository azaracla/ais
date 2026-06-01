#!/bin/bash
set -euo pipefail

# Load environment variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../.env"

LOCAL_CATALOG="/tmp/ais.ducklake"

# Parameters: DATE (required), HOUR (required, 00-23)
DATE=${1:-"$(date -d "yesterday" +%Y-%m-%d)"}
HOUR=${2:-"00"}  # Default to hour 00 if not specified

echo "📦 Consolidating data for date: ${DATE} hour: ${HOUR}"

# Validate hour
if ! [[ "$HOUR" =~ ^[0-9]{2}$ ]] && ! [[ "$HOUR" =~ ^[0-9]$ ]]; then
    echo "❌ Error: HOUR must be 00-23"
    exit 1
fi

# Pad hour to 2 digits
HOUR_PADDED=$(printf "%02d" "$((10#$HOUR))" 2>/dev/null || echo "00")

# Create final SQL file by combining config + consolidate.sql
TMP_SQL=$(mktemp /tmp/consolidate_final_XXXXXX.sql)
trap "rm -f $TMP_SQL" EXIT

# Write header with S3 config and ATTACH
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

# Append consolidate.sql with date and hour parameters replaced
sed "s/:target_date/'${DATE}'/g; s/:target_hour/${HOUR_PADDED}/g" "${SCRIPT_DIR}/sql/consolidate.sql" >> "${TMP_SQL}"

# Execute the combined SQL file
duckdb -f "${TMP_SQL}"

echo "✅ Consolidation terminée pour ${DATE} ${HOUR}:00"

# Auto-save catalog to S3
"${SCRIPT_DIR}/save_catalog.sh" > /dev/null 2>&1 || true
