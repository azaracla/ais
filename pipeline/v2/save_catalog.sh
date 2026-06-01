#!/bin/bash
set -euo pipefail

# Save local DuckLake catalog file to S3 for read-only access by other processes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../.env"

LOCAL_CATALOG="/tmp/ais.ducklake"
S3_CATALOG="s3://${BUCKET_PUBLIC}/v2/ais.ducklake"

echo "💾 Saving local catalog to S3..."
echo "From: ${LOCAL_CATALOG}"
echo "To:   ${S3_CATALOG}"

# Export AWS CLI compatible environment variables
export AWS_ACCESS_KEY_ID="${OVH_ACCESS_KEY}"
export AWS_SECRET_ACCESS_KEY="${OVH_SECRET_KEY}"
export AWS_ENDPOINT_URL="${OVH_ENDPOINT}"
export AWS_REGION="${OVH_REGION}"

# Upload catalog file to S3
aws s3 cp "${LOCAL_CATALOG}" "${S3_CATALOG}" 2>&1 || \
    echo "⚠️  AWS CLI upload failed. Manual upload needed: aws --endpoint-url=${OVH_ENDPOINT} s3 cp ${LOCAL_CATALOG} ${S3_CATALOG}"

# Set public-read ACL on catalog for direct HTTP access
aws s3api put-object-acl --bucket "${BUCKET_PUBLIC}" --key "v2/ais.ducklake" --acl public-read 2>&1 || \
    echo "⚠️  Failed to set public-read ACL. Manual command: aws --endpoint-url=${OVH_ENDPOINT} --region ${OVH_REGION} s3api put-object-acl --bucket ${BUCKET_PUBLIC} --key v2/ais.ducklake --acl public-read"

echo "✅ Catalog saved to S3 with public-read ACL"

# Set public-read ACL on data files (parquet) for direct HTTP access from DuckDB-WASM
echo "📁 Setting public-read ACL on data files..."
aws s3api list-objects --bucket "${BUCKET_PUBLIC}" --prefix "v2/ais.ducklake.files/" \
    --query "Contents[].Key" --output text 2>/dev/null | tr '\t' '\n' | \
    xargs -I{} aws s3api put-object-acl --bucket "${BUCKET_PUBLIC}" --key "{}" --acl public-read 2>&1 || \
    echo "⚠️  Failed to set public-read ACL on some data files"
echo ""
echo "Direct public access URL: ducklake:https://${BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net/v2/ais.ducklake"
echo ""
echo "Test direct access with:"
echo "  duckdb ducklake:https://${BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net/v2/ais.ducklake -c \"SELECT COUNT(*) FROM messages;\""
echo ""
echo "Or attach in DuckDB with:"
echo "  ATTACH 's3://${BUCKET_PUBLIC}/v2/ais.ducklake' AS ais_lake (TYPE ducklake, DATA_PATH 's3://${BUCKET_PUBLIC}/v2/ais.ducklake.files/', OVERRIDE_DATA_PATH true);"
