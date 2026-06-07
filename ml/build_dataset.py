"""Build ML training dataset v6 — predict time-to-arrival.

New v6 features:
  - navigational_status (one-hot): 0=underway, 1=anchor, 5=moored, etc.
  - heading_offset: |COG - true_heading| (drift/sideslip signal)
  - rate_of_turn: turning intensity
  - closing_speed_kmh: rate of dist_to_dest decrease between consecutive samples
  - approach_efficiency: closing_speed / SOG (0=sideways, 1=straight)
"""

import duckdb
from utils import DATA_DIR

ARRIVALS = DATA_DIR / "arrivals.parquet"
POSITIONS = DATA_DIR / "positions_filtered.parquet"
VESSELS_URL = (
    "https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake.files/"
    "gold/vessels/vessels.parquet"
)
OUTPUT = DATA_DIR / "dataset.parquet"

SAMPLE_HOURS_BEFORE = [0.5, 1, 2, 3, 6, 12, 24, 48, 72, 120, 168]
SAMPLE_WINDOW_MIN = 5

# AIS navigational status codes and meanings
NAV_STATUS_MAP = {
    0: "underway_engine", 1: "at_anchor", 2: "not_under_command",
    3: "restricted_maneuver", 4: "constrained_draught", 5: "moored",
    6: "aground", 7: "fishing", 8: "underway_sail",
    9: "reserved_9", 10: "reserved_10", 11: "reserved_11",
    12: "reserved_12", 13: "reserved_13", 14: "reserved_14",
    15: "not_defined",
}


def build():
    print("Building training dataset v6 (time-to-arrival) ...")
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

    # ── Load positions (now includes navigational_status, rate_of_turn) ──
    print("Loading positions ...")
    con.execute(f"""
        CREATE TABLE positions AS
        SELECT mmsi, ts, lat, lon, sog, cog, true_heading, navigational_status, rate_of_turn
        FROM read_parquet('{POSITIONS}')
        WHERE sog IS NOT NULL
    """)
    n_pos = con.execute("SELECT count(*) FROM positions").fetchone()[0]
    print(f"  {n_pos} positions")

    # ── Historical SOG features ──
    print("Computing historical SOG features ...")
    con.execute("""
        CREATE TABLE positions_hist AS
        SELECT
            mmsi, ts, lat, lon, sog, cog, true_heading, navigational_status, rate_of_turn,
            COALESCE(AVG(sog) OVER (PARTITION BY mmsi ORDER BY EXTRACT(EPOCH FROM ts)
                     RANGE BETWEEN 3600 PRECEDING AND CURRENT ROW), sog) AS avg_sog_1h,
            COALESCE(AVG(sog) OVER (PARTITION BY mmsi ORDER BY EXTRACT(EPOCH FROM ts)
                     RANGE BETWEEN 21600 PRECEDING AND CURRENT ROW), sog) AS avg_sog_6h,
            COALESCE(AVG(sog) OVER (PARTITION BY mmsi ORDER BY EXTRACT(EPOCH FROM ts)
                     RANGE BETWEEN 86400 PRECEDING AND CURRENT ROW), sog) AS avg_sog_24h,
            COALESCE(sog - AVG(sog) OVER (PARTITION BY mmsi ORDER BY EXTRACT(EPOCH FROM ts)
                       RANGE BETWEEN 3600 PRECEDING AND 1 PRECEDING), 0.0) AS sog_trend_1h,
            -- Heading stability: variance of heading over last hour (511=unknown → skip)
            COALESCE(
                STDDEV_SAMP(CASE WHEN true_heading < 360 THEN true_heading ELSE NULL END) OVER (
                    PARTITION BY mmsi ORDER BY EXTRACT(EPOCH FROM ts)
                    RANGE BETWEEN 3600 PRECEDING AND CURRENT ROW
                ), 0.0
            ) AS heading_std_1h,
            -- Average heading over last hour
            COALESCE(
                AVG(CASE WHEN true_heading < 360 THEN true_heading ELSE NULL END) OVER (
                    PARTITION BY mmsi ORDER BY EXTRACT(EPOCH FROM ts)
                    RANGE BETWEEN 3600 PRECEDING AND CURRENT ROW
                ), CASE WHEN true_heading < 360 THEN true_heading ELSE -1 END
            ) AS avg_heading_1h,
            AVG(sog) OVER (PARTITION BY mmsi) AS mmsi_avg_sog
        FROM positions
    """)

    n_hist = con.execute("SELECT count(*) FROM positions_hist").fetchone()[0]
    print(f"  {n_hist} positions with history")

    # ── Join arrivals with positions ──
    print("Joining positions with arrivals ...")
    con.execute("""
        CREATE TABLE traj AS
        SELECT
            a.mmsi, a.arrival_ts, a.arrival_lat, a.arrival_lon,
            a.port_lo_code, a.port_lat, a.port_lon,
            a.destination_clean, a.detection_method,
            p.ts AS pos_ts, p.lat AS pos_lat, p.lon AS pos_lon,
            p.sog AS pos_sog, p.cog AS pos_cog,
            p.true_heading, p.navigational_status, p.rate_of_turn,
            p.avg_sog_1h, p.avg_sog_6h, p.avg_sog_24h, p.sog_trend_1h,
            p.heading_std_1h, p.avg_heading_1h, p.mmsi_avg_sog,
            DATEDIFF('second', p.ts, a.arrival_ts) / 3600.0 AS time_to_arrival_hours
        FROM arrivals a
        JOIN positions_hist p ON a.mmsi = p.mmsi
        WHERE p.ts <= a.arrival_ts - INTERVAL '5' MINUTE
          AND p.ts >= a.arrival_ts - INTERVAL '7' DAY
    """)
    n_traj = con.execute("SELECT count(*) FROM traj").fetchone()[0]
    print(f"  {n_traj} position-arrival pairs")

    # ── Sample positions at target hours ──
    sample_cols = [
        "mmsi", "arrival_ts", "arrival_lat", "arrival_lon",
        "port_lo_code", "port_lat", "port_lon",
        "destination_clean", "detection_method",
        "pos_ts", "pos_lat", "pos_lon", "pos_sog", "pos_cog",
        "true_heading", "navigational_status", "rate_of_turn",
        "avg_sog_1h", "avg_sog_6h", "avg_sog_24h", "sog_trend_1h",
        "heading_std_1h", "avg_heading_1h", "mmsi_avg_sog",
        "time_to_arrival_hours",
    ]
    sample_cols_str = ", ".join(sample_cols)

    print(f"Sampling positions at {len(SAMPLE_HOURS_BEFORE)} time horizons ...")
    sampled_rows = []
    for target_h in SAMPLE_HOURS_BEFORE:
        t_min = target_h - SAMPLE_WINDOW_MIN / 60.0
        t_max = target_h + SAMPLE_WINDOW_MIN / 60.0
        rows = con.execute(f"""
            SELECT DISTINCT ON (mmsi, arrival_ts)
                {sample_cols_str}
            FROM traj
            WHERE time_to_arrival_hours BETWEEN {t_min} AND {t_max}
            ORDER BY mmsi, arrival_ts, ABS(time_to_arrival_hours - {target_h}) ASC
        """).fetchall()
        sampled_rows.extend(rows)
        print(f"    ~{target_h:4.0f}h: {len(rows)} samples")

    print(f"  Total samples: {len(sampled_rows)}")

    # ── Create samples table via Polars → Parquet ──
    import polars as pl
    df_samples = pl.DataFrame(sampled_rows, schema=sample_cols, orient="row")
    df_samples.write_parquet(DATA_DIR / "_samples.parquet")
    con.execute(f"CREATE TABLE samples AS SELECT * FROM read_parquet('{DATA_DIR / '_samples.parquet'}')")

    # ── Spatial features in Polars (vectorized) ──
    import numpy as np
    df_sv = pl.read_parquet(DATA_DIR / "_samples.parquet")
    vessels_df = con.execute("SELECT mmsi, ship_type, length, width FROM vessels").pl()
    df_sv = df_sv.join(vessels_df, on="mmsi", how="left").with_columns([
        pl.col("length").fill_null(0).alias("vessel_length"),
        pl.col("width").fill_null(0).alias("vessel_width"),
    ])

    # Haversine distance
    def haversine_vec(lat1, lon1, lat2, lon2):
        r = 6371.0
        dlat = np.radians(lat2 - lat1)
        dlon = np.radians(lon2 - lon1)
        a = np.sin(dlat / 2) ** 2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2) ** 2
        return 2 * r * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

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

    # Heading offset: difference between COG and true_heading (vessel pointing vs moving)
    th = df_sv["true_heading"].to_numpy()
    # 511 = AIS "not available"
    th_valid = th.copy()
    th_valid = np.where(th >= 360, np.nan, th_valid)
    heading_offset = np.abs(cog - th_valid)
    # Normalize to [0, 180]
    heading_offset = np.where(heading_offset > 180, 360 - heading_offset, heading_offset)
    heading_offset = np.where(np.isnan(heading_offset), -1, heading_offset)  # -1 = unknown
    df_sv = df_sv.with_columns(pl.Series("heading_offset_deg", heading_offset))

    # Rate of turn: -128 = AIS "not available"
    rot = df_sv["rate_of_turn"].to_numpy()
    rot_clean = np.where(rot == -128, 0, rot).astype(np.float32)  # -128 → 0 (no turn)
    # Also mark "no data" as a feature
    rot_available = (rot != -128).astype(np.float32)
    df_sv = df_sv.with_columns([
        pl.Series("rate_of_turn_clean", rot_clean),
        pl.Series("rot_available", rot_available),
    ])

    # Navigational status: clean and prepare for one-hot
    nav = df_sv["navigational_status"].to_numpy()
    nav_clean = np.where(np.isnan(nav), -1, nav).astype(np.int32)
    df_sv = df_sv.with_columns(pl.Series("nav_status", nav_clean))

    df_sv.write_parquet(DATA_DIR / "_samples_enriched.parquet")
    con.execute("DROP TABLE IF EXISTS samples_enriched")
    con.execute(f"CREATE TABLE samples_enriched AS SELECT * FROM read_parquet('{DATA_DIR / '_samples_enriched.parquet'}')")

    # ── Closing speed from consecutive samples (trajectory feature) ──
    # For each vessel+arrival, compute how fast dist_to_dest decreases between samples
    print("Computing closing speed ...")
    con.execute("""
        CREATE TABLE samples_with_close AS
        SELECT *,
            -- Previous sample's distance
            LAG(dist_to_dest_km) OVER (
                PARTITION BY mmsi, arrival_ts ORDER BY pos_ts
            ) AS prev_dist_to_dest,
            -- Previous sample's timestamp
            LAG(pos_ts) OVER (
                PARTITION BY mmsi, arrival_ts ORDER BY pos_ts
            ) AS prev_ts,
            -- Previous sample's SOG
            LAG(pos_sog) OVER (
                PARTITION BY mmsi, arrival_ts ORDER BY pos_ts
            ) AS prev_sog
        FROM samples_enriched
    """)
    # Compute closing speed: (prev_dist - curr_dist) / time_gap_hours
    con.execute("""
        CREATE TABLE samples_enriched_v2 AS
        SELECT *,
            CASE
                WHEN prev_dist_to_dest IS NOT NULL AND prev_ts IS NOT NULL
                THEN ROUND(
                    (prev_dist_to_dest - dist_to_dest_km) /
                    GREATEST(EXTRACT(EPOCH FROM pos_ts - prev_ts) / 3600.0, 0.01),
                2)
                ELSE 0
            END AS closing_speed_kmh,
            CASE
                WHEN prev_sog IS NOT NULL AND prev_sog > 0.5
                    AND prev_dist_to_dest IS NOT NULL AND prev_ts IS NOT NULL
                THEN ROUND(
                    (prev_dist_to_dest - dist_to_dest_km) /
                    GREATEST(EXTRACT(EPOCH FROM pos_ts - prev_ts) / 3600.0, 0.01) /
                    GREATEST(prev_sog, 0.5),
                3)
                ELSE NULL
            END AS approach_efficiency
        FROM samples_with_close
    """)

    # ── Final dataset with all features ──
    con.execute(f"""
        CREATE TABLE dataset AS
        SELECT
            mmsi, port_lo_code, destination_clean,
            arrival_ts, pos_ts,
            time_to_arrival_hours,
            dist_to_dest_km,
            pos_sog AS sog,
            pos_cog AS cog,
            bearing_offset_deg,
            heading_offset_deg,
            rate_of_turn_clean AS rate_of_turn,
            rot_available,
            nav_status,
            ship_type, vessel_length, vessel_width,
            CASE WHEN vessel_width > 0 AND vessel_length > 0
                 THEN ROUND(vessel_length / vessel_width, 2)
                 ELSE 0 END AS length_width_ratio,
            EXTRACT(HOUR FROM pos_ts) AS hour_of_day,
            EXTRACT(DAYOFWEEK FROM pos_ts) AS day_of_week,
            COALESCE(avg_sog_1h, pos_sog) AS avg_sog_1h,
            COALESCE(avg_sog_6h, pos_sog) AS avg_sog_6h,
            COALESCE(avg_sog_24h, pos_sog) AS avg_sog_24h,
            COALESCE(sog_trend_1h, 0) AS sog_trend_1h,
            COALESCE(mmsi_avg_sog, pos_sog) AS mmsi_avg_sog,
            COALESCE(heading_std_1h, 0) AS heading_std_1h,
            COALESCE(avg_heading_1h, -1) AS avg_heading_1h,
            COALESCE(closing_speed_kmh, 0) AS closing_speed_kmh,
            COALESCE(approach_efficiency, 0) AS approach_efficiency,
            ROUND(dist_to_dest_km / GREATEST(pos_sog, 1.0), 2) AS eta_naive_h,
            ROUND(pos_sog / GREATEST(mmsi_avg_sog, 1.0), 3) AS sog_vs_mmsi_avg,
            detection_method
        FROM samples_enriched_v2
        WHERE dist_to_dest_km IS NOT NULL
          AND dist_to_dest_km > 0
          AND dist_to_dest_km < 20000
          AND time_to_arrival_hours > 0
          AND time_to_arrival_hours < 200
          AND pos_sog > 0
          AND pos_sog < 50
    """)

    n_final = con.execute("SELECT count(*) FROM dataset").fetchone()[0]
    print(f"  Clean dataset: {n_final} rows")

    # Export
    con.execute(f"""
        COPY (SELECT * FROM dataset ORDER BY mmsi, pos_ts)
        TO '{OUTPUT}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    print(f"Exported to {OUTPUT}")

    # Stats
    stats = con.execute("""
        SELECT count(*) AS n,
            ROUND(AVG(time_to_arrival_hours), 1) AS avg_tta,
            ROUND(AVG(dist_to_dest_km), 1) AS avg_dist,
            ROUND(AVG(sog), 1) AS avg_sog,
            ROUND(AVG(closing_speed_kmh), 1) AS avg_close,
            ROUND(AVG(approach_efficiency), 3) AS avg_eff
        FROM dataset
    """).fetchone()
    print(f"\nDataset v6 stats: rows={stats[0]} avg_tta={stats[1]}h avg_dist={stats[2]}km avg_close={stats[4]}km/h avg_eff={stats[5]}")

    # Navigational status distribution
    print("\nNavigational status distribution:")
    for row in con.execute("""
        SELECT nav_status, count(*) AS n
        FROM dataset
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12
    """).fetchall():
        label = NAV_STATUS_MAP.get(int(row[0]) if row[0] is not None else -1, f"unknown_{row[0]}")
        print(f"    {int(row[0]) if row[0] is not None else 'NULL':>4} ({label:25s}): {row[1]}")

    print("\nSamples per time horizon:")
    for row in con.execute("""
        SELECT CASE
            WHEN time_to_arrival_hours <= 1 THEN '0-1h'
            WHEN time_to_arrival_hours <= 6 THEN '1-6h'
            WHEN time_to_arrival_hours <= 24 THEN '6-24h'
            WHEN time_to_arrival_hours <= 72 THEN '1-3d'
            WHEN time_to_arrival_hours <= 168 THEN '3-7d'
            ELSE '>7d' END AS horizon, count(*) AS n
        FROM dataset GROUP BY horizon ORDER BY MIN(time_to_arrival_hours)
    """).fetchall():
        print(f"    {row[0]}: {row[1]}")

    con.close()
    return n_final


if __name__ == "__main__":
    build()
