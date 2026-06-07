"""Build ML training dataset v2 — predict time-to-arrival.

Target: time_to_arrival_hours = arrival_ts - current_position_ts.
Features: distance, speed, course, vessel characteristics, time context.

Samples multiple positions along each vessel's trajectory before arrival.
No dependency on declared ETA → model works for any vessel with a destination.
"""

import math
import duckdb
from utils import DATA_DIR, haversine_km

ARRIVALS = DATA_DIR / "arrivals.parquet"
POSITIONS = DATA_DIR / "positions_filtered.parquet"
VESSELS_URL = (
    "https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake.files/"
    "gold/vessels/vessels.parquet"
)
OUTPUT = DATA_DIR / "dataset.parquet"

# Sampling: take positions at these hours before arrival
SAMPLE_HOURS_BEFORE = [0.5, 1, 2, 3, 6, 12, 24, 48, 72, 120, 168]
SAMPLE_WINDOW_MIN = 5  # minutes tolerance around target time


def build():
    print("Building training dataset v2 (time-to-arrival) ...")
    con = duckdb.connect()

    # ── Load arrivals ──
    print("Loading arrivals ...")
    con.execute(f"CREATE TABLE arrivals AS SELECT * FROM read_parquet('{ARRIVALS}')")
    n_arr = con.execute("SELECT count(*) FROM arrivals").fetchone()[0]
    n_mmsi = con.execute("SELECT count(DISTINCT mmsi) FROM arrivals").fetchone()[0]
    print(f"  {n_arr} arrivals, {n_mmsi} distinct MMSIs")

    # ── Load vessels ──
    print("Loading vessel catalog ...")
    con.execute(f"CREATE TABLE vessels AS SELECT mmsi, ship_type, length, width FROM read_parquet('{VESSELS_URL}')")

    # ── Load positions ──
    print("Loading positions ...")
    con.execute(f"""
        CREATE TABLE positions AS
        SELECT mmsi, ts, lat, lon, sog, cog
        FROM read_parquet('{POSITIONS}')
        WHERE sog IS NOT NULL
    """)
    n_pos = con.execute("SELECT count(*) FROM positions").fetchone()[0]
    print(f"  {n_pos} positions")

    # ── Join arrivals with positions ──
    # Keep positions in the window [arrival_ts - 7 days, arrival_ts - 5 min]
    print("Joining positions with arrivals ...")
    con.execute("""
        CREATE TABLE traj AS
        SELECT
            a.mmsi,
            a.arrival_ts,
            a.arrival_lat,
            a.arrival_lon,
            a.port_lo_code,
            a.port_lat,
            a.port_lon,
            a.destination_clean,
            a.detection_method,
            p.ts AS pos_ts,
            p.lat AS pos_lat,
            p.lon AS pos_lon,
            p.sog AS pos_sog,
            p.cog AS pos_cog,
            DATEDIFF('second', p.ts, a.arrival_ts) / 3600.0 AS time_to_arrival_hours,
            -- Only keep positions before arrival and within 7 days
        FROM arrivals a
        JOIN positions p ON a.mmsi = p.mmsi
        WHERE p.ts <= a.arrival_ts - INTERVAL '5' MINUTE
          AND p.ts >= a.arrival_ts - INTERVAL '7' DAY
    """)
    n_traj = con.execute("SELECT count(*) FROM traj").fetchone()[0]
    print(f"  {n_traj} position-arrival pairs")

    # ── Sample positions at target hours before arrival ──
    print(f"Sampling positions at {len(SAMPLE_HOURS_BEFORE)} time horizons ...")
    sampled_rows = []
    for target_h in SAMPLE_HOURS_BEFORE:
        t_min = target_h - SAMPLE_WINDOW_MIN / 60.0
        t_max = target_h + SAMPLE_WINDOW_MIN / 60.0
        rows = con.execute(f"""
            SELECT DISTINCT ON (mmsi, arrival_ts)
                mmsi, arrival_ts, arrival_lat, arrival_lon,
                port_lo_code, port_lat, port_lon, destination_clean, detection_method,
                pos_ts, pos_lat, pos_lon, pos_sog, pos_cog, time_to_arrival_hours
            FROM traj
            WHERE time_to_arrival_hours BETWEEN {t_min} AND {t_max}
            ORDER BY mmsi, arrival_ts, ABS(time_to_arrival_hours - {target_h}) ASC
        """).fetchall()
        sampled_rows.extend(rows)
        print(f"    ~{target_h:4.0f}h: {len(rows)} samples")

    print(f"  Total samples: {len(sampled_rows)}")

    # ── Create dataset table via Polars → Parquet (avoids DuckDB param limits) ──
    import polars as pl
    columns = [
        "mmsi", "arrival_ts", "arrival_lat", "arrival_lon",
        "port_lo_code", "port_lat", "port_lon",
        "destination_clean", "detection_method",
        "pos_ts", "pos_lat", "pos_lon", "pos_sog", "pos_cog",
        "time_to_arrival_hours",
    ]
    df_samples = pl.DataFrame(sampled_rows, schema=columns, orient="row")
    df_samples.write_parquet(DATA_DIR / "_samples.parquet")
    con.execute(f"CREATE TABLE samples AS SELECT * FROM read_parquet('{DATA_DIR / '_samples.parquet'}')")

    # ── Add vessel features ──
    con.execute("""
        CREATE TABLE samples_vessel AS
        SELECT s.*, v.ship_type, COALESCE(v.length, 0) AS vessel_length, COALESCE(v.width, 0) AS vessel_width
        FROM samples s
        LEFT JOIN vessels v ON s.mmsi = v.mmsi
    """)

    # ── Compute spatial features in Python (vectorized via polars) ──
    import polars as pl
    df_sv = pl.read_parquet(DATA_DIR / "_samples.parquet")
    # Join with vessels
    vessels_df = con.execute("SELECT mmsi, ship_type, length, width FROM vessels").pl()
    df_sv = df_sv.join(vessels_df, on="mmsi", how="left").with_columns([
        pl.col("length").fill_null(0).alias("vessel_length"),
        pl.col("width").fill_null(0).alias("vessel_width"),
    ])

    # Compute haversine distance and bearing offset
    def haversine_vec(lat1, lon1, lat2, lon2):
        """Vectorized haversine (km)."""
        r = 6371.0
        dlat = np.radians(lat2 - lat1)
        dlon = np.radians(lon2 - lon1)
        a = np.sin(dlat / 2) ** 2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2) ** 2
        return 2 * r * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

    import numpy as np
    d = haversine_vec(
        df_sv["pos_lat"].to_numpy(), df_sv["pos_lon"].to_numpy(),
        df_sv["port_lat"].to_numpy(), df_sv["port_lon"].to_numpy(),
    )
    df_sv = df_sv.with_columns(pl.Series("dist_to_dest_km", d))

    # Bearing offset
    plat = np.radians(df_sv["pos_lat"].to_numpy())
    plon = np.radians(df_sv["pos_lon"].to_numpy())
    dlat = np.radians(df_sv["port_lat"].to_numpy())
    dlon = np.radians(df_sv["port_lon"].to_numpy())
    y = np.sin(dlon - plon) * np.cos(dlat)
    x = np.cos(plat) * np.sin(dlat) - np.sin(plat) * np.cos(dlat) * np.cos(dlon - plon)
    direct_bearing = (np.arctan2(y, x) * 180.0 / np.pi + 360) % 360
    cog = df_sv["pos_cog"].to_numpy()
    offset = np.abs(cog - direct_bearing)
    offset = np.where(offset > 180, 360 - offset, offset)
    df_sv = df_sv.with_columns(pl.Series("bearing_offset_deg", offset))

    # Write enriched dataset back
    df_sv.write_parquet(DATA_DIR / "_samples_enriched.parquet")
    con.execute(f"DROP TABLE IF EXISTS samples_vessel")
    con.execute(f"CREATE TABLE samples_vessel AS SELECT * FROM read_parquet('{DATA_DIR / '_samples_enriched.parquet'}')")

    # ── Add time features and filter ──
    con.execute("""
        CREATE TABLE dataset AS
        SELECT
            mmsi, port_lo_code, destination_clean,
            arrival_ts, pos_ts,
            time_to_arrival_hours,
            dist_to_dest_km,
            pos_sog AS sog,
            pos_cog AS cog,
            bearing_offset_deg,
            ship_type, vessel_length, vessel_width,
            CASE WHEN vessel_width > 0 AND vessel_length > 0
                 THEN ROUND(vessel_length / vessel_width, 2)
                 ELSE 0 END AS length_width_ratio,
            EXTRACT(HOUR FROM pos_ts) AS hour_of_day,
            EXTRACT(DAYOFWEEK FROM pos_ts) AS day_of_week,
            detection_method
        FROM samples_vessel
        WHERE dist_to_dest_km IS NOT NULL
          AND dist_to_dest_km > 0
          AND dist_to_dest_km < 20000  -- reasonable max distance
          AND time_to_arrival_hours > 0
          AND time_to_arrival_hours < 200  -- max ~8 days
          AND pos_sog > 0  -- vessel must be moving (stopped = already arrived)
          AND pos_sog < 50  -- reasonable max speed
    """)

    n_final = con.execute("SELECT count(*) FROM dataset").fetchone()[0]
    print(f"  Clean dataset: {n_final} rows")

    # ── Export ──
    con.execute(f"""
        COPY (SELECT * FROM dataset ORDER BY mmsi, pos_ts)
        TO '{OUTPUT}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    print(f"Exported to {OUTPUT}")

    # ── Stats ──
    stats = con.execute("""
        SELECT
            count(*) AS n,
            ROUND(AVG(time_to_arrival_hours), 1) AS avg_tta_h,
            ROUND(STDDEV(time_to_arrival_hours), 1) AS std_tta_h,
            ROUND(AVG(dist_to_dest_km), 1) AS avg_dist_km,
            ROUND(AVG(sog), 1) AS avg_sog,
            ROUND(AVG(bearing_offset_deg), 1) AS avg_bearing_off
        FROM dataset
    """).fetchone()
    print(f"""
Dataset v2 stats:
  Rows:           {stats[0]}
  Time-to-arrival: mean={stats[1]}h  std={stats[2]}h
  Avg distance:   {stats[3]} km
  Avg SOG:        {stats[4]} kn
  Avg bearing off:{stats[5]}°
""")

    # By time horizon
    print("Samples per time horizon:")
    for row in con.execute("""
        SELECT
            CASE
                WHEN time_to_arrival_hours <= 1 THEN '0-1h'
                WHEN time_to_arrival_hours <= 6 THEN '1-6h'
                WHEN time_to_arrival_hours <= 24 THEN '6-24h'
                WHEN time_to_arrival_hours <= 72 THEN '1-3d'
                WHEN time_to_arrival_hours <= 168 THEN '3-7d'
                ELSE '>7d'
            END AS horizon,
            count(*) AS n
        FROM dataset
        GROUP BY horizon ORDER BY MIN(time_to_arrival_hours)
    """).fetchall():
        print(f"    {row[0]}: {row[1]}")

    con.close()
    return n_final


if __name__ == "__main__":
    build()
