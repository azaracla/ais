-- v3 Vessels From Scratch: extract vessels from today's silver only.
-- Used when no existing vessels.parquet exists on S3 (first ever run).
-- Parameters:
--   :silver_path — path to messages_consolidated.parquet (today's data)
--   :output_path — path to write vessels.parquet

COPY (
    SELECT
        mmsi, name, call_sign, imo_number, ship_type,
        length, width, destination, ts AS last_seen_static
    FROM read_parquet(:silver_path)
    WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
      AND name IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY ts DESC) = 1
    ORDER BY mmsi
) TO :output_path (FORMAT 'PARQUET', COMPRESSION 'ZSTD');
