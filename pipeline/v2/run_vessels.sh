#!/bin/bash
set -euo pipefail

# Nettoyer les fichiers temporaires
rm -f /tmp/vessels_*.sql

# Load environment variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../.env"

LOCAL_CATALOG="/tmp/ais.ducklake"

echo "🚢 Updating vessels dimension table"

# Create final SQL file
TMP_SQL=$(mktemp /tmp/vessels_final_XXXXXX.sql)
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

# Append update_vessels.sql
cat "${SCRIPT_DIR}/sql/update_vessels.sql" >> "${TMP_SQL}"

# Execute
duckdb -f "${TMP_SQL}"

echo "✅ Mise à jour de 'vessels' terminée"

# Auto-save catalog to S3
"${SCRIPT_DIR}/save_catalog.sh" > /dev/null 2>&1 || true
