-- v3 Port Congestion: hourly aggregate from port_calls
-- Computes for each (port, hour): vessels in port, arrivals, departures.
-- Recomputes entirely each day from the full port_calls table (tiny data).
-- Parameters:
--   :port_calls_path    — path to port_calls.parquet (merged, all time)
--   :output_path         — output path for port_congestion.parquet

COPY (
    WITH bounds AS (
        SELECT
            MIN(arrival_ts) AS min_ts,
            COALESCE(MAX(departure_ts), MAX(arrival_ts)) AS max_ts
        FROM read_parquet(:port_calls_path)
    ),
    hours AS (
        SELECT UNNEST(
            GENERATE_SERIES(
                DATE_TRUNC('hour', min_ts)::TIMESTAMPTZ,
                DATE_TRUNC('hour', max_ts)::TIMESTAMPTZ,
                INTERVAL 1 HOUR
            )
        ) AS hour
        FROM bounds
    ),
    ports AS (
        SELECT DISTINCT port_lo_code
        FROM read_parquet(:port_calls_path)
        WHERE port_lo_code != ''
    ),
    grid AS (
        SELECT p.port_lo_code, h.hour
        FROM ports p CROSS JOIN hours h
    ),
    calls AS (
        SELECT * FROM read_parquet(:port_calls_path)
    ),
    hourly AS (
        SELECT
            g.port_lo_code,
            g.hour,
            COUNT(CASE WHEN c.arrival_ts <= g.hour
                        AND (c.departure_ts IS NULL OR c.departure_ts >= g.hour)
                   THEN 1 END)::BIGINT AS vessels_in_port,
            COUNT(CASE WHEN c.arrival_ts >= g.hour
                        AND c.arrival_ts < g.hour + INTERVAL 1 HOUR
                   THEN 1 END)::BIGINT AS arrivals,
            COUNT(CASE WHEN c.departure_ts >= g.hour
                        AND c.departure_ts < g.hour + INTERVAL 1 HOUR
                   THEN 1 END)::BIGINT AS departures
        FROM grid g
        LEFT JOIN calls c ON c.port_lo_code = g.port_lo_code
        GROUP BY g.port_lo_code, g.hour
    )
    SELECT
        port_lo_code,
        hour,
        vessels_in_port,
        arrivals,
        departures,
        CAST(hour AS DATE) AS date
    FROM hourly
    WHERE vessels_in_port > 0 OR arrivals > 0 OR departures > 0
    ORDER BY port_lo_code, hour
) TO :output_path (FORMAT 'PARQUET', COMPRESSION 'ZSTD');
