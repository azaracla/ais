-- DuckLake v2 Initialization
-- Catalog: s3://ais-public-prod/v2/ais.ducklake
-- Data files: s3://ais-public-prod/v2/ais.ducklake.files/

-- 1. Load DuckLake extension
INSTALL ducklake;
LOAD ducklake;

-- 2. Attach the DuckLake catalog
ATTACH 's3://ais-public-prod/v2/ais.ducklake' AS ais_lake (
    TYPE ducklake,
    DATA_PATH 's3://ais-public-prod/v2/ais.ducklake.files/',
    OVERRIDE_DATA_PATH true
);

-- 3. Create raw messages table (bronze layer)
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
    metadata_json VARCHAR,
    year INTEGER GENERATED ALWAYS AS (EXTRACT(year FROM ts)),
    month INTEGER GENERATED ALWAYS AS (EXTRACT(month FROM ts)),
    day INTEGER GENERATED ALWAYS AS (EXTRACT(day FROM ts))
) PARTITIONED BY (year, month, day);

-- 4. Create gold layer tables

-- Vessels positions (position messages only)
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
) PARTITIONED BY (year, month, day);

-- Vessel tracks (downsampled for visualization)
CREATE TABLE IF NOT EXISTS ais_lake.vessel_tracks (
    mmsi INTEGER,
    ts INTEGER,
    lat INTEGER,
    lon INTEGER,
    date DATE
) PARTITIONED BY (date);

-- Base stations
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
) PARTITIONED BY (year, month, day);

-- Aids to navigation
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
) PARTITIONED BY (year, month, day);

-- Static vessel information (dimension table)
CREATE TABLE IF NOT EXISTS ais_lake.vessels (
    mmsi BIGINT PRIMARY KEY,
    name VARCHAR,
    call_sign VARCHAR,
    imo_number BIGINT,
    ship_type INTEGER,
    length DOUBLE,
    width DOUBLE,
    destination VARCHAR,
    last_seen_static TIMESTAMPTZ
);

-- 5. S3 Configuration (to be set via environment variables in scripts)
-- SET s3_endpoint='s3.gra.io.cloud.ovh.net';
-- SET s3_access_key_id='${OVH_ACCESS_KEY}';
-- SET s3_secret_access_key='${OVH_SECRET_KEY}';
-- SET s3_region='gra';
-- SET s3_url_style='path';
-- SET s3_use_ssl=true;
