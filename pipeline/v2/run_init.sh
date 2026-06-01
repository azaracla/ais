#!/bin/bash
set -euo pipefail

# Load environment variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../../.env"

LOCAL_CATALOG="/tmp/ais.ducklake"

echo "🔧 Initializing DuckLake v2 (catalog: ${LOCAL_CATALOG}, data: S3)"

# Télécharger le catalogue depuis S3 si non présent localement
if [ ! -f "${LOCAL_CATALOG}" ]; then
    echo "📥 Catalogue non trouvé localement, téléchargement depuis S3..."
    "${SCRIPT_DIR}/load_catalog.sh" 2>/dev/null || \
        echo "⚠️  Premier lancement ou S3 non accessible - création d'un nouveau catalogue"
fi

# Create temp SQL file with S3 configuration
TMP_SQL=$(mktemp /tmp/init_ducklake_XXXXXX.sql)
trap "rm -f $TMP_SQL" EXIT

# Generate SQL with S3 configuration
cat > "${TMP_SQL}" <<'EOF'
INSTALL httpfs;
LOAD httpfs;
INSTALL ducklake;
LOAD ducklake;
EOF

# Add S3 configuration to temp file
cat >> "${TMP_SQL}" <<EOF

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

-- Create raw messages table (bronze layer)
CREATE TABLE IF NOT EXISTS ais_lake.messages (
    message_type VARCHAR,
    mmsi BIGINT,
    ts TIMESTAMPTZ,
    lat DOUBLE,
    lon DOUBLE,
    received_at TIMESTAMPTZ,
    source_listener VARCHAR,
    sog DOUBLE,
    cog DOUBLE,
    true_heading INTEGER,
    navigational_status INTEGER,
    rate_of_turn INTEGER,
    message_id INTEGER,
    position_accuracy BOOLEAN,
    raim BOOLEAN,
    valid BOOLEAN,
    name VARCHAR,
    call_sign VARCHAR,
    imo_number BIGINT,
    ship_type INTEGER,
    ais_version INTEGER,
    length DOUBLE,
    width DOUBLE,
    dimension_a DOUBLE,
    dimension_b DOUBLE,
    dimension_c DOUBLE,
    dimension_d DOUBLE,
    max_static_draught DOUBLE,
    destination VARCHAR,
    eta TIMESTAMPTZ,
    dte BOOLEAN,
    fix_type INTEGER,
    type_of_aton INTEGER,
    off_position BOOLEAN,
    virtual_aton BOOLEAN,
    raw_message VARCHAR,
    metadata_json VARCHAR
);

-- Set partitioning for messages (DuckLake uses ALTER TABLE, not PARTITIONED BY in CREATE)
ALTER TABLE ais_lake.messages SET PARTITIONED BY (year(ts), month(ts), day(ts));

-- Create gold layer tables
CREATE TABLE IF NOT EXISTS ais_lake.vessels_positions (
    message_type VARCHAR,
    mmsi BIGINT,
    ts TIMESTAMPTZ,
    lat DOUBLE,
    lon DOUBLE,
    received_at TIMESTAMPTZ,
    source_listener VARCHAR,
    sog DOUBLE,
    cog DOUBLE,
    true_heading INTEGER,
    navigational_status INTEGER,
    rate_of_turn INTEGER,
    message_id INTEGER,
    position_accuracy BOOLEAN,
    raim BOOLEAN,
    valid BOOLEAN,
    year INTEGER,
    month INTEGER,
    day INTEGER
);
ALTER TABLE ais_lake.vessels_positions SET PARTITIONED BY (year, month, day);

CREATE TABLE IF NOT EXISTS ais_lake.vessel_tracks (
    mmsi INTEGER,
    ts INTEGER,
    lat INTEGER,
    lon INTEGER,
    date DATE
);
ALTER TABLE ais_lake.vessel_tracks SET PARTITIONED BY (date);

CREATE TABLE IF NOT EXISTS ais_lake.base_stations (
    mmsi BIGINT,
    ts TIMESTAMPTZ,
    lat DOUBLE,
    lon DOUBLE,
    received_at TIMESTAMPTZ,
    source_listener VARCHAR,
    message_id INTEGER,
    raim BOOLEAN,
    year INTEGER,
    month INTEGER,
    day INTEGER
);
ALTER TABLE ais_lake.base_stations SET PARTITIONED BY (year, month, day);

CREATE TABLE IF NOT EXISTS ais_lake.aids_to_navigation (
    mmsi BIGINT,
    name VARCHAR,
    type_of_aton INTEGER,
    ts TIMESTAMPTZ,
    lat DOUBLE,
    lon DOUBLE,
    dimension_a DOUBLE,
    dimension_b DOUBLE,
    dimension_c DOUBLE,
    dimension_d DOUBLE,
    off_position BOOLEAN,
    virtual_aton BOOLEAN,
    raim BOOLEAN,
    received_at TIMESTAMPTZ,
    source_listener VARCHAR,
    year INTEGER,
    month INTEGER,
    day INTEGER
);
ALTER TABLE ais_lake.aids_to_navigation SET PARTITIONED BY (year, month, day);

-- Static vessel information (dimension table)
-- Note: DuckLake does not support PRIMARY KEY constraints
CREATE TABLE IF NOT EXISTS ais_lake.vessels (
    mmsi BIGINT,
    name VARCHAR,
    call_sign VARCHAR,
    imo_number BIGINT,
    ship_type INTEGER,
    length DOUBLE,
    width DOUBLE,
    destination VARCHAR,
    last_seen_static TIMESTAMPTZ
);
EOF

# Execute the SQL file
duckdb -f "${TMP_SQL}"

echo "✅ DuckLake v2 initialization complete (catalog: ${LOCAL_CATALOG})"

# Auto-save catalog to S3
"${SCRIPT_DIR}/save_catalog.sh" > /dev/null 2>&1 || true
