-- v3 Consolidation: NDJSON.zst → messages_consolidated.parquet (local)
-- Reads raw NDJSON.zst from LOCAL disk (downloaded in parallel by Python).
--
-- DuckDB infers a single UNION STRUCT for all message types in the 'message' MAP.
-- Every field reference must exist in that STRUCT (bind-time check).
-- ReportA only has: Name, Valid.  ReportB has: CallSign, Dimension, FixType,
-- ShipType, Spare, Valid, VenderIDModel, VenderIDSerial, VendorIDName.
--
-- Use dot notation (metadata.field) instead of ->> to avoid DuckDB bugs with
-- non-existent STRUCT keys in OR/COALESCE expressions.
--
-- Parameters:
--   :raw_path    — local glob for .ndjson.zst files
--   :output_path — local path for output Parquet

SET memory_limit = '10GB';
SET temp_directory = '/tmp/duckdb_v3_tmp';
SET preserve_insertion_order = false;

COPY (
    SELECT
        message_type,

        -- MMSI: metadata.MMSI is always a BIGINT (never a placeholder string).
        -- Fallback to UserID in the message body (some AIS messages have MMSI there).
        COALESCE(
            metadata.MMSI,
            (message[message_type]['UserID'])::BIGINT
        ) AS mmsi,

        -- Timestamp: metadata.time_utc is VARCHAR "2026-05-26 14:32:35.123 +0000 UTC".
        -- Strip " UTC" and " +0000" suffix then cast.  Fallback: bare cast.
        CASE
            WHEN metadata.time_utc LIKE '% UTC' THEN
                replace(replace(metadata.time_utc, ' UTC', ''), ' +0000', '')::TIMESTAMPTZ
            ELSE try(metadata.time_utc::TIMESTAMPTZ)
        END AS ts,

        metadata.latitude::DOUBLE AS lat,
        metadata.longitude::DOUBLE AS lon,
        received_at::TIMESTAMPTZ AS received_at,
        listener_id AS source_listener,

        -- Dynamic fields from message[message_type]
        (message[message_type]['Sog'])::DOUBLE AS sog,
        (message[message_type]['Cog'])::DOUBLE AS cog,
        (message[message_type]['TrueHeading'])::INTEGER AS true_heading,
        (message[message_type]['NavigationalStatus'])::INTEGER AS navigational_status,
        (message[message_type]['RateOfTurn'])::INTEGER AS rate_of_turn,
        (message[message_type]['MessageID'])::INTEGER AS message_id,
        (message[message_type]['PositionAccuracy'])::BOOLEAN AS position_accuracy,
        (message[message_type]['Raim'])::BOOLEAN AS raim,
        (message[message_type]['Valid'])::BOOLEAN AS valid,

        -- Name: direct access covers ShipStaticData, AidsToNavigationReport.
        -- For StaticDataReport, name is in ReportA.Name.
        COALESCE(
            (message[message_type]['Name'])::VARCHAR,
            (message['StaticDataReport']['ReportA']['Name'])::VARCHAR,
            NULLIF(metadata.ShipName, '')::VARCHAR
        ) AS name,

        -- CallSign: direct or in ReportB (ReportB HAS CallSign).
        COALESCE(
            (message[message_type]['CallSign'])::VARCHAR,
            (message['StaticDataReport']['ReportB']['CallSign'])::VARCHAR
        ) AS call_sign,

        -- ImoNumber: only directly on the struct (ReportA does NOT have it).
        (message[message_type]['ImoNumber'])::BIGINT AS imo_number,

        -- ShipType: direct (field name 'Type') or ReportB.ShipType.
        COALESCE(
            (message[message_type]['Type'])::INTEGER,
            (message['StaticDataReport']['ReportB']['ShipType'])::INTEGER
        ) AS ship_type,

        (message[message_type]['AisVersion'])::INTEGER AS ais_version,

        -- Dimensions: direct first, then ReportB.Dimension (StaticDataReport puts
        -- dimensions under ReportB).  Default 0 for length/width computation.
        COALESCE(
            (message[message_type]['Dimension']['A'])::DOUBLE,
            (message['StaticDataReport']['ReportB']['Dimension']['A'])::DOUBLE,
            0
        ) AS dimension_a,
        COALESCE(
            (message[message_type]['Dimension']['B'])::DOUBLE,
            (message['StaticDataReport']['ReportB']['Dimension']['B'])::DOUBLE,
            0
        ) AS dimension_b,
        COALESCE(
            (message[message_type]['Dimension']['C'])::DOUBLE,
            (message['StaticDataReport']['ReportB']['Dimension']['C'])::DOUBLE,
            0
        ) AS dimension_c,
        COALESCE(
            (message[message_type]['Dimension']['D'])::DOUBLE,
            (message['StaticDataReport']['ReportB']['Dimension']['D'])::DOUBLE,
            0
        ) AS dimension_d,

        -- length = A + B, width = C + D
        COALESCE(
            (message[message_type]['Dimension']['A'])::DOUBLE,
            (message['StaticDataReport']['ReportB']['Dimension']['A'])::DOUBLE,
            0
        ) + COALESCE(
            (message[message_type]['Dimension']['B'])::DOUBLE,
            (message['StaticDataReport']['ReportB']['Dimension']['B'])::DOUBLE,
            0
        ) AS length,
        COALESCE(
            (message[message_type]['Dimension']['C'])::DOUBLE,
            (message['StaticDataReport']['ReportB']['Dimension']['C'])::DOUBLE,
            0
        ) + COALESCE(
            (message[message_type]['Dimension']['D'])::DOUBLE,
            (message['StaticDataReport']['ReportB']['Dimension']['D'])::DOUBLE,
            0
        ) AS width,

        (message[message_type]['MaximumStaticDraught'])::DOUBLE AS max_static_draught,
        (message[message_type]['Destination'])::VARCHAR AS destination,

        NULL::TIMESTAMPTZ AS eta,
        (message[message_type]['Dte'])::BOOLEAN AS dte,

        -- FixType: AidsToNavigationReport uses 'Fixtype' (lowercase 't').
        COALESCE(
            (message[message_type]['FixType'])::INTEGER,
            (message[message_type]['Fixtype'])::INTEGER
        ) AS fix_type,

        CASE WHEN message_type = 'AidsToNavigationReport'
            THEN (message[message_type]['Type'])::INTEGER
            ELSE NULL
        END AS type_of_aton,

        (message[message_type]['OffPosition'])::BOOLEAN AS off_position,
        (message[message_type]['VirtualAtoN'])::BOOLEAN AS virtual_aton,

        NULL::VARCHAR AS raw_message,
        NULL::VARCHAR AS metadata_json,

        EXTRACT(year FROM ts) AS year,
        EXTRACT(month FROM ts) AS month,
        EXTRACT(day FROM ts) AS day
    FROM read_ndjson(:raw_path, ignore_errors = true)
    WHERE metadata.MMSI IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY mmsi, ts, message_type
        ORDER BY received_at ASC
    ) = 1
    ORDER BY message_type ASC, mmsi ASC, ts ASC
) TO :output_path (FORMAT 'PARQUET', COMPRESSION 'ZSTD', ROW_GROUP_SIZE 100000);
