-- Derive Gold Layer Tables for DuckLake v2
-- Parameters: :target_date (format: YYYY-MM-DD), :target_hour (format: HH, 00-23)
-- Note: Filters by hour for incremental processing

-- 1. vessels_positions - All position messages
INSERT INTO ais_lake.vessels_positions
SELECT 
    message_type, mmsi, ts, lat, lon, received_at, source_listener,
    sog, cog, true_heading, navigational_status, rate_of_turn,
    message_id, position_accuracy, raim, valid,
    EXTRACT(year FROM ts) AS year,
    EXTRACT(month FROM ts) AS month,
    EXTRACT(day FROM ts) AS day
FROM ais_lake.messages
WHERE message_type IN (
    'PositionReport',
    'ExtendedClassBPositionReport',
    'StandardClassBPositionReport',
    'LongRangeAisBroadcast'
)
AND EXTRACT(year FROM ts) = EXTRACT(year FROM CAST(:target_date AS DATE))
AND EXTRACT(month FROM ts) = EXTRACT(month FROM CAST(:target_date AS DATE))
AND EXTRACT(day FROM ts) = EXTRACT(day FROM CAST(:target_date AS DATE))
AND EXTRACT(hour FROM ts) = :target_hour::INTEGER;

-- 2. vessel_tracks - Downsampled to 10-minute intervals for visualization
INSERT INTO ais_lake.vessel_tracks
SELECT 
    mmsi::INTEGER AS mmsi,
    epoch(ts)::INTEGER AS ts,
    CAST(ROUND(lat * 1e5) AS INTEGER) AS lat,
    CAST(ROUND(lon * 1e5) AS INTEGER) AS lon,
    CAST(ts AS DATE) AS date
FROM (
    SELECT 
        mmsi, ts, lat, lon,
        epoch(ts)::INTEGER // 600 AS _bucket,  -- 600 seconds = 10 minutes
        ROW_NUMBER() OVER (PARTITION BY mmsi, epoch(ts)::INTEGER // 600 ORDER BY ts ASC) AS _rn
    FROM ais_lake.messages
    WHERE message_type IN (
        'PositionReport',
        'ExtendedClassBPositionReport',
        'StandardClassBPositionReport'
    )
    AND EXTRACT(year FROM ts) = EXTRACT(year FROM CAST(:target_date AS DATE))
    AND EXTRACT(month FROM ts) = EXTRACT(month FROM CAST(:target_date AS DATE))
    AND EXTRACT(day FROM ts) = EXTRACT(day FROM CAST(:target_date AS DATE))
    AND EXTRACT(hour FROM ts) = :target_hour::INTEGER
) WHERE _rn = 1;

-- 3. base_stations - Base station reports
INSERT INTO ais_lake.base_stations
SELECT 
    mmsi, ts, lat, lon, received_at, source_listener, message_id, raim,
    EXTRACT(year FROM ts) AS year,
    EXTRACT(month FROM ts) AS month,
    EXTRACT(day FROM ts) AS day
FROM ais_lake.messages
WHERE message_type = 'BaseStationReport'
AND EXTRACT(year FROM ts) = EXTRACT(year FROM CAST(:target_date AS DATE))
AND EXTRACT(month FROM ts) = EXTRACT(month FROM CAST(:target_date AS DATE))
AND EXTRACT(day FROM ts) = EXTRACT(day FROM CAST(:target_date AS DATE))
AND EXTRACT(hour FROM ts) = :target_hour::INTEGER;

-- 4. aids_to_navigation - Aids to navigation reports
INSERT INTO ais_lake.aids_to_navigation
SELECT 
    mmsi, name, type_of_aton, ts, lat, lon,
    dimension_a, dimension_b, dimension_c, dimension_d,
    off_position, virtual_aton, raim, received_at, source_listener,
    EXTRACT(year FROM ts) AS year,
    EXTRACT(month FROM ts) AS month,
    EXTRACT(day FROM ts) AS day
FROM ais_lake.messages
WHERE message_type = 'AidsToNavigationReport'
AND EXTRACT(year FROM ts) = EXTRACT(year FROM CAST(:target_date AS DATE))
AND EXTRACT(month FROM ts) = EXTRACT(month FROM CAST(:target_date AS DATE))
AND EXTRACT(day FROM ts) = EXTRACT(day FROM CAST(:target_date AS DATE))
AND EXTRACT(hour FROM ts) = :target_hour::INTEGER;
