-- v3 Vessels Merge: merge today's new/updated vessels with existing vessels table.
-- Downloads existing vessels.parquet from S3, merges with today's silver,
-- deduplicates by MMSI keeping the most recent last_seen_static.
-- If no existing vessels file exists, extracts from today's silver only.
-- Parameters:
--   :silver_path — path to messages_consolidated.parquet (today's data)
--   :existing_vessels_path — path to existing vessels.parquet (downloaded from S3)
--   :output_path — path to write merged vessels.parquet

COPY (
    WITH today AS (
        SELECT
            mmsi, name, call_sign, imo_number, ship_type,
            length, width, destination, ts AS last_seen_static
        FROM read_parquet(:silver_path)
        WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
          AND name IS NOT NULL
    ),
    merged AS (
        SELECT * FROM today
        UNION ALL
        SELECT * FROM read_parquet(:existing_vessels_path)
    )
    SELECT * FROM merged
    QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY last_seen_static DESC) = 1
    ORDER BY mmsi
) TO :output_path (FORMAT 'PARQUET', COMPRESSION 'ZSTD');
