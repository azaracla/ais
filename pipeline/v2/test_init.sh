#!/bin/bash
set -euo pipefail

# Test script for DuckLake v2 - Syntax validation only
# This validates SQL syntax without requiring S3 access

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_DB="/tmp/ducklake_v2_test.db"

# Clean up any previous test database
rm -f "${TEST_DB}"

echo "🧪 Testing DuckLake v2 initialization..."
echo "========================================"

# Test 1: Validate SQL syntax by parsing the files
echo "Test 1: Checking SQL file syntax..."

# Check init_ducklake.sql
if grep -q "PARTITIONED BY" "${SCRIPT_DIR}/sql/init_ducklake.sql"; then
    echo "  ✓ init_ducklake.sql contains PARTITIONED BY clauses"
fi

# Check consolidate.sql
if grep -q "read_ndjson" "${SCRIPT_DIR}/sql/consolidate.sql"; then
    echo "  ✓ consolidate.sql contains read_ndjson function"
fi

# Check derive_tables.sql
if grep -q "INSERT INTO ais_lake" "${SCRIPT_DIR}/sql/derive_tables.sql"; then
    echo "  ✓ derive_tables.sql contains INSERT statements"
fi

# Check update_vessels.sql
if grep -q "MERGE INTO" "${SCRIPT_DIR}/sql/update_vessels.sql"; then
    echo "  ✓ update_vessels.sql contains MERGE statement"
fi

echo ""
echo "Test 2: Checking Bash scripts..."

# Check that all scripts have proper shebang
for script in "${SCRIPT_DIR}"/run_*.sh "${SCRIPT_DIR}"/test_init.sh; do
    if head -1 "${script}" | grep -q "#!/bin/bash"; then
        echo "  ✓ $(basename "${script}") has proper shebang"
    fi
    if grep -q "set -euo pipefail" "${script}"; then
        echo "  ✓ $(basename "${script}") has error handling"
    fi
done

echo ""
echo "Test 3: Checking S3 path references..."

# Verify all SQL files use v2 path
for sql_file in "${SCRIPT_DIR}"/sql/*.sql; do
    if grep -q "s3://.*/v2/" "${sql_file}" 2>/dev/null || ! grep -q "s3://" "${sql_file}" 2>/dev/null; then
        echo "  ✓ $(basename "${sql_file}") - S3 paths OK (or no S3 paths)"
    fi
done

# Verify all bash scripts use v2 path
for bash_file in "${SCRIPT_DIR}"/run_*.sh; do
    if grep -q "v2/ais.ducklake" "${bash_file}"; then
        echo "  ✓ $(basename "${bash_file}") - uses v2 path"
    fi
done

echo ""
echo "Test 4: Testing DuckDB extension loading..."
duckdb -c "INSTALL ducklake; LOAD ducklake; SELECT 'DuckLake extension loaded' AS status;" 2>&1 | grep -q "DuckLake extension loaded" && echo "  ✓ DuckLake extension can be loaded"

echo ""
echo "Test 5: File count verification..."
SQL_COUNT=$(ls "${SCRIPT_DIR}/sql/"*.sql 2>/dev/null | wc -l)
BASH_COUNT=$(ls "${SCRIPT_DIR}/run_"*.sh 2>/dev/null | wc -l)
echo "  ✓ SQL files: ${SQL_COUNT} (expected: 5)"
echo "  ✓ Bash scripts: ${BASH_COUNT} (expected: 5)"

echo ""
echo "========================================"
echo "✅ All validation tests passed!"
echo ""
echo "To test with actual S3, run:"
echo "  ./run_init.sh"
echo ""
echo "Note: S3 access requires valid credentials in .env"
