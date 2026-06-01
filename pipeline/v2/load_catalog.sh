#!/bin/bash
set -euo pipefail

# Load DuckLake catalog from S3 to local for read-only queries

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../.env"

LOCAL_CATALOG="/tmp/ais.ducklake"
S3_CATALOG="s3://${BUCKET_PUBLIC}/v2/ais.ducklake"

echo "📥 Loading catalog from S3 to local..."
echo "From: ${S3_CATALOG}"
echo "To:   ${LOCAL_CATALOG}"

# Export AWS CLI compatible environment variables
export AWS_ACCESS_KEY_ID="${OVH_ACCESS_KEY}"
export AWS_SECRET_ACCESS_KEY="${OVH_SECRET_KEY}"
export AWS_ENDPOINT_URL="${OVH_ENDPOINT}"
export AWS_REGION="${OVH_REGION}"

# Download catalog file from S3
aws s3 cp "${S3_CATALOG}" "${LOCAL_CATALOG}" 2>&1 || \
    echo "⚠️  AWS CLI download failed. Manual download needed: aws --endpoint-url=${OVH_ENDPOINT} s3 cp ${S3_CATALOG} ${LOCAL_CATALOG}"

echo "✅ Catalog loaded to ${LOCAL_CATALOG}"
echo ""
echo "Now you can query with:"
echo "  duckdb -c \"ATTACH '${LOCAL_CATALOG}' AS ais_lake (TYPE ducklake, DATA_PATH 's3://${BUCKET_PUBLIC}/v2/ais.ducklake.files/', OVERRIDE_DATA_PATH true); SELECT COUNT(*) FROM ais_lake.messages;\""
