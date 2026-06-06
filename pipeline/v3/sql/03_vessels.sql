-- v3 Vessels: extract today's new/updated vessels from silver.
-- COPY TO local (same pattern as gold tables), uploaded + registered.
-- Does NOT use DuckLake DELETE+INSERT — avoids ACL issues, keeps files in gold/.
-- Parameters:
--   :silver_path — path to messages_consolidated.parquet (today's data)

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
