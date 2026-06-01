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
    -- Dynamic fields (from MAP keyed by message_type)
    (message[message_type]['Sog']::DOUBLE) AS sog,
    (message[message_type]['Cog']::DOUBLE) AS cog,
    (message[message_type]['TrueHeading']::INTEGER) AS true_heading,
    (message[message_type]['NavigationalStatus']::INTEGER) AS navigational_status,
    (message[message_type]['RateOfTurn']::INTEGER) AS rate_of_turn,
    (message[message_type]['MessageID']::INTEGER) AS message_id,
    (message[message_type]['PositionAccuracy']::BOOLEAN) AS position_accuracy,
    (message[message_type]['Raim']::BOOLEAN) AS raim,
    (message[message_type]['Valid']::BOOLEAN) AS valid,
    -- Static data
    COALESCE(message[message_type]['Name']::VARCHAR, metadata.ShipName::VARCHAR) AS name,
    COALESCE(message[message_type]['CallSign']::VARCHAR, metadata.CallSign::VARCHAR) AS call_sign,
    COALESCE((message[message_type]['ImoNumber'])::BIGINT, metadata.ImoNumber::BIGINT) AS imo_number,
    COALESCE((message[message_type]['Type'])::INTEGER) AS ship_type,
    (message[message_type]['AisVersion']::INTEGER) AS ais_version,
    -- Dimensions
    (COALESCE((message[message_type]['Dimension']['A'])::DOUBLE, 0) + COALESCE((message[message_type]['Dimension']['B'])::DOUBLE, 0)) AS length,
    (COALESCE((message[message_type]['Dimension']['C'])::DOUBLE, 0) + COALESCE((message[message_type]['Dimension']['D'])::DOUBLE, 0)) AS width,
    (message[message_type]['Dimension']['A'])::DOUBLE AS dimension_a,
    (message[message_type]['Dimension']['B'])::DOUBLE AS dimension_b,
    (message[message_type]['Dimension']['C'])::DOUBLE AS dimension_c,
    (message[message_type]['Dimension']['D'])::DOUBLE AS dimension_d,
    (message[message_type]['MaximumStaticDraught'])::DOUBLE AS max_static_draught,
    -- Destination and ETA
    COALESCE(message[message_type]['Destination']::VARCHAR, metadata.Destination::VARCHAR) AS destination,
    (message[message_type]['Eta']::TIMESTAMPTZ) AS eta,
    (message[message_type]['Dte']::BOOLEAN) AS dte,
    COALESCE((message[message_type]['FixType'])::INTEGER) AS fix_type,
    -- AtoN specific
    CASE WHEN message_type = 'AidsToNavigationReport' THEN 
        (message[message_type]['Type'])::INTEGER
    ELSE NULL END AS type_of_aton,
    (message[message_type]['OffPosition']::BOOLEAN) AS off_position,
    (message[message_type]['VirtualAtoN']::BOOLEAN) AS virtual_aton,
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
