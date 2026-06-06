-- v3 Vessels Upsert: update vessels dimension from today's silver data.
-- Runs with DuckLake attached (S3 DATA_PATH) — only operation that touches S3.
-- Parameters:
--   :silver_path — path to messages_consolidated.parquet (local, today's data)

-- Remove vessels that have new static data today, then re-insert.
-- Historical vessels not seen today are left untouched.
DELETE FROM ais_lake.vessels
WHERE mmsi IN (
    SELECT DISTINCT mmsi
    FROM read_parquet(:silver_path)
    WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
      AND name IS NOT NULL
);

INSERT INTO ais_lake.vessels
SELECT
    mmsi, name, call_sign, imo_number, ship_type,
    length, width, destination, ts AS last_seen_static
FROM read_parquet(:silver_path)
WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
  AND name IS NOT NULL
QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY ts DESC) = 1;
