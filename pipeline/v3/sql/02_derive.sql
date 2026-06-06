-- v3 Derive Gold Tables: messages_consolidated.parquet → 4 tables gold (local)
-- Reads consolidated Parquet locally, writes gold Parquet locally.
-- Uses Hive-style directory paths so DuckLake can discover partitions.
-- Parameters:
--   :silver_path              — path to messages_consolidated.parquet
--   :vessels_positions_path   — e.g. gold/vessels_positions/year=2026/month=06/day=05/vessels_positions.parquet
--   :vessel_tracks_path       — e.g. gold/vessel_tracks/date=2026-06-05/vessel_tracks.parquet
--   :base_stations_path       — e.g. gold/base_stations/year=2026/month=06/day=05/base_stations.parquet
--   :aids_to_navigation_path  — e.g. gold/aids_to_navigation/year=2026/month=06/day=05/aids_to_navigation.parquet

-- 1. vessels_positions — all position message types
-- Sorted by ts (temporal) because frontend query always filters by 10-min time
-- window first. ts sort gives 1-RG prune vs spatial sort scanning all RGs in bbox.
COPY (
    SELECT
        message_type, mmsi, ts, lat, lon, received_at, source_listener,
        sog, cog, true_heading, navigational_status, rate_of_turn,
        message_id, position_accuracy, raim, valid,
        year, month, day
    FROM read_parquet(:silver_path)
    WHERE message_type IN (
        'PositionReport', 'ExtendedClassBPositionReport',
        'StandardClassBPositionReport', 'LongRangeAisBroadcast'
    )
    ORDER BY ts ASC, mmsi ASC
) TO :vessels_positions_path
  (FORMAT 'PARQUET', COMPRESSION 'ZSTD', ROW_GROUP_SIZE 100000);

-- 2. vessel_tracks — downsampled 10-min positions with integer coords
COPY (
    SELECT
        mmsi::INTEGER                              AS mmsi,
        epoch(ts)::INTEGER                         AS ts,
        CAST(ROUND(lat * 1e5) AS INTEGER)          AS lat,
        CAST(ROUND(lon * 1e5) AS INTEGER)          AS lon,
        CAST(ts AS DATE)                           AS date
    FROM (
        SELECT mmsi, ts, lat, lon,
               epoch(ts)::INTEGER // 600 AS _bucket,
               ROW_NUMBER() OVER (
                   PARTITION BY mmsi, epoch(ts)::INTEGER // 600
                   ORDER BY ts ASC
               ) AS _rn
        FROM read_parquet(:silver_path)
        WHERE message_type IN (
            'PositionReport', 'ExtendedClassBPositionReport',
            'StandardClassBPositionReport'
        )
    ) WHERE _rn = 1
    ORDER BY mmsi ASC, ts ASC
) TO :vessel_tracks_path
  (FORMAT 'PARQUET', COMPRESSION 'ZSTD', COMPRESSION_LEVEL 6, ROW_GROUP_SIZE 100000);

-- 3. base_stations
COPY (
    SELECT
        mmsi, ts, lat, lon, received_at, source_listener,
        message_id, raim,
        year, month, day
    FROM read_parquet(:silver_path)
    WHERE message_type = 'BaseStationReport'
    ORDER BY ts ASC, mmsi ASC
) TO :base_stations_path
  (FORMAT 'PARQUET', COMPRESSION 'ZSTD', ROW_GROUP_SIZE 100000);

-- 4. aids_to_navigation
COPY (
    SELECT
        mmsi, name, type_of_aton, ts, lat, lon,
        dimension_a, dimension_b, dimension_c, dimension_d,
        off_position, virtual_aton, raim,
        received_at, source_listener,
        year, month, day
    FROM read_parquet(:silver_path)
    WHERE message_type = 'AidsToNavigationReport'
    ORDER BY ts ASC, mmsi ASC
) TO :aids_to_navigation_path
  (FORMAT 'PARQUET', COMPRESSION 'ZSTD', ROW_GROUP_SIZE 100000);
