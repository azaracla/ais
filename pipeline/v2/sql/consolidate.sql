-- Consolidation SQL for DuckLake v2
-- Reads NDJSON.zst files from ais-raw-prod and inserts into ais_lake.messages
-- Parameters:
--   :target_date (format: YYYY-MM-DD)
--   :target_hour (format: HH, optional - if empty, processes all hours)

-- Read raw NDJSON.zst files and insert into messages table
INSERT INTO ais_lake.messages
SELECT 
    message_type,
    -- MMSI: try multiple variants, cast each to BIGINT
    COALESCE(
        (NULLIF(metadata->>'MMSI', 'MMSI'))::BIGINT,
        (NULLIF(metadata->>'mmsi', 'mmsi'))::BIGINT,
        (message->>'MMSI')::BIGINT
    ) AS mmsi,
    -- Timestamp: handle format "YYYY-MM-DD HH:MM:SS.us +0000 UTC"
    CASE 
        WHEN metadata->>'time_utc' LIKE '% UTC' THEN 
            replace(replace(metadata->>'time_utc', ' UTC', ''), ' +0000', '')::TIMESTAMPTZ
        WHEN metadata->>'timestamp' LIKE '% UTC' THEN 
            replace(replace(metadata->>'timestamp', ' UTC', ''), ' +0000', '')::TIMESTAMPTZ
        ELSE COALESCE(
            (metadata->>'time_utc')::TIMESTAMPTZ,
            (metadata->>'timestamp')::TIMESTAMPTZ
        )
    END AS ts,
    -- Coordinates
    COALESCE(
        (metadata->>'latitude')::DOUBLE,
        (metadata->>'lat')::DOUBLE
    ) AS lat,
    COALESCE(
        (metadata->>'longitude')::DOUBLE,
        (metadata->>'lon')::DOUBLE
    ) AS lon,
    -- Received at
    COALESCE(
        (metadata->>'received_at')::TIMESTAMPTZ,
        received_at::TIMESTAMPTZ
    ) AS received_at,
    COALESCE(metadata->>'listener_id', listener_id) AS source_listener,
    -- Dynamic fields
    COALESCE((message->>'Sog')::DOUBLE, (metadata->>'sog')::DOUBLE) AS sog,
    COALESCE((message->>'Cog')::DOUBLE, (metadata->>'cog')::DOUBLE) AS cog,
    COALESCE((message->>'TrueHeading')::INTEGER, (message->>'trueHeading')::INTEGER) AS true_heading,
    COALESCE((message->>'NavigationalStatus')::INTEGER, (message->>'navigationalStatus')::INTEGER) AS navigational_status,
    COALESCE((message->>'RateOfTurn')::INTEGER, (message->>'rateOfTurn')::INTEGER) AS rate_of_turn,
    COALESCE((message->>'MessageID')::INTEGER, (message->>'messageId')::INTEGER) AS message_id,
    COALESCE((message->>'PositionAccuracy')::BOOLEAN, (message->>'positionAccuracy')::BOOLEAN) AS position_accuracy,
    COALESCE((message->>'Raim')::BOOLEAN, (message->>'raim')::BOOLEAN) AS raim,
    COALESCE((message->>'Valid')::BOOLEAN, (message->>'valid')::BOOLEAN) AS valid,
    -- Static data
    COALESCE(message->>'Name', metadata->>'name') AS name,
    COALESCE(message->>'CallSign', metadata->>'callSign') AS call_sign,
    COALESCE((message->>'ImoNumber')::BIGINT, (metadata->>'imoNumber')::BIGINT) AS imo_number,
    COALESCE((message->>'Type')::INTEGER, (message->>'shipType')::INTEGER) AS ship_type,
    (message->>'AisVersion')::INTEGER AS ais_version,
    -- Dimensions
    (COALESCE((message->'Dimension'->>'A')::DOUBLE, 0) + COALESCE((message->'Dimension'->>'B')::DOUBLE, 0)) AS length,
    (COALESCE((message->'Dimension'->>'C')::DOUBLE, 0) + COALESCE((message->'Dimension'->>'D')::DOUBLE, 0)) AS width,
    (message->'Dimension'->>'A')::DOUBLE AS dimension_a,
    (message->'Dimension'->>'B')::DOUBLE AS dimension_b,
    (message->'Dimension'->>'C')::DOUBLE AS dimension_c,
    (message->'Dimension'->>'D')::DOUBLE AS dimension_d,
    (message->>'MaximumStaticDraught')::DOUBLE AS max_static_draught,
    -- Destination and ETA
    COALESCE(message->>'Destination', metadata->>'destination') AS destination,
    CASE 
        WHEN message->>'Eta' IS NOT NULL THEN 
            CASE 
                WHEN message->>'Eta' ~ '^\\d{4}-\\d{2}-\\d{2}' THEN (message->>'Eta')::TIMESTAMPTZ
                WHEN message->>'Eta' ~ '^\\d{2}/\\d{2}/\\d{4}' THEN strptime(message->>'Eta', '%d/%m/%Y')
                WHEN message->>'Eta' ~ '^\\d+$' THEN to_timestamp((message->>'Eta')::BIGINT)
                ELSE NULL 
            END
        ELSE NULL 
    END AS eta,
    (message->>'Dte')::BOOLEAN AS dte,
    COALESCE((message->>'FixType')::INTEGER, (message->>'fixType')::INTEGER) AS fix_type,
    -- AtoN specific
    CASE WHEN message_type = 'AidsToNavigationReport' THEN 
        COALESCE((message->>'Type')::INTEGER, (message->>'typeOfAton')::INTEGER) 
    ELSE NULL END AS type_of_aton,
    (message->>'OffPosition')::BOOLEAN AS off_position,
    (message->>'VirtualAtoN')::BOOLEAN AS virtual_aton,
    -- Raw data (placeholder)
    NULL::VARCHAR AS raw_message,
    NULL::VARCHAR AS metadata_json
FROM read_ndjson(
    's3://ais-raw-prod/raw/year=' || EXTRACT(year FROM CAST(:target_date AS DATE)) ||
    '/month=' || LPAD(EXTRACT(month FROM CAST(:target_date AS DATE))::VARCHAR, 2, '0') ||
    '/day=' || LPAD(EXTRACT(day FROM CAST(:target_date AS DATE))::VARCHAR, 2, '0') ||
    '/hour=' || LPAD(:target_hour::VARCHAR, 2, '0') || '/*.ndjson.zst',
    hive_partitioning=true,
    ignore_errors=true
)
-- Remove duplicates: keep the first message received for each (mmsi, ts, message_type)
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY mmsi, ts, message_type
    ORDER BY received_at ASC
) = 1;
