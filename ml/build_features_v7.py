"""Build v7 features — trajectory context, port, MMSI, draught, physics.

Strategy: compute trajectory features via range join (sample ↔ positions in [pos_ts-6h, pos_ts]).
This is ~198K samples × ~100 positions each = ~20M intermediate rows (vs 97M before).
Memory-efficient and parallelized by DuckDB.

Usage:
  uv run python ml/build_features_v7.py              # full build
  uv run python ml/build_features_v7.py --sample 1000  # test on 1000 samples
"""

import sys
import duckdb
import numpy as np
import polars as pl

from utils import DATA_DIR, connect_catalog

DATASET_V6 = DATA_DIR / "dataset.parquet"
POSITIONS = DATA_DIR / "positions_filtered.parquet"
ARRIVALS = DATA_DIR / "arrivals.parquet"
OUTPUT = DATA_DIR / "dataset_v7.parquet"

SAMPLE_N = None
for arg in sys.argv:
    if arg == "--sample" or arg.startswith("--sample="):
        idx = sys.argv.index(arg)
        if "=" in arg:
            SAMPLE_N = int(arg.split("=")[1])
        elif idx + 1 < len(sys.argv):
            SAMPLE_N = int(sys.argv[idx + 1])
        break


def build():
    print("=" * 70)
    print("Building v7 Dataset — Range-join trajectory features")
    if SAMPLE_N:
        print(f"  TEST MODE: {SAMPLE_N} samples only")
    print("=" * 70)

    con = duckdb.connect()

    # ── Load dataset samples ──
    print("\n── Loading v6 dataset ──")
    con.execute(f"CREATE TABLE samples AS SELECT * FROM read_parquet('{DATASET_V6}')")
    n_all = con.execute("SELECT count(*) FROM samples").fetchone()[0]

    if SAMPLE_N:
        # Take a random subset of MMSIs
        mmsis = con.execute("SELECT DISTINCT mmsi FROM samples USING SAMPLE 200").fetchall()
        mmsi_list = ",".join(str(r[0]) for r in mmsis)
        con.execute(f"CREATE TABLE samples_sub AS SELECT * FROM samples WHERE mmsi IN ({mmsi_list})")
        con.execute("DROP TABLE samples")
        con.execute("ALTER TABLE samples_sub RENAME TO samples")

    n = con.execute("SELECT count(*) FROM samples").fetchone()[0]
    n_mmsi = con.execute("SELECT count(DISTINCT mmsi) FROM samples").fetchone()[0]
    print(f"  {n} samples from {n_mmsi} MMSIs")

    # ── Trajectory features via range join ──
    print("\n── Computing trajectory features (range join to positions) ──")
    con.execute(f"""
        CREATE TABLE traj_raw AS
        SELECT
            s.mmsi, s.arrival_ts, s.pos_ts,
            s.time_to_arrival_hours, s.dist_to_dest_km,
            s.sog AS cur_sog, s.cog AS cur_cog,
            p.ts AS p_ts, p.sog AS p_sog, p.cog AS p_cog,
            p.true_heading AS p_heading
        FROM samples s
        JOIN read_parquet('{POSITIONS}') p
            ON s.mmsi = p.mmsi
            AND p.ts >= s.pos_ts - INTERVAL '6' HOUR
            AND p.ts <= s.pos_ts
    """)
    n_joined = con.execute("SELECT count(*) FROM traj_raw").fetchone()[0]
    print(f"  Joined: {n_joined} rows ({n_joined/n:.0f}x per sample)")

    # Aggregate trajectory features per sample
    print("  Aggregating ...")
    # First add LAG for COG changes
    con.execute("""
        CREATE TABLE traj_ordered AS
        SELECT *,
            LAG(p_cog) OVER (PARTITION BY mmsi, pos_ts ORDER BY p_ts) AS prev_cog
        FROM traj_raw
    """)
    con.execute("""
        CREATE TABLE traj_agg AS
        SELECT
            mmsi, pos_ts, arrival_ts,
            cur_sog,
            -- Stop/slow fractions
            AVG(CASE WHEN p_sog < 1.0 THEN 1.0 ELSE 0.0 END) AS stop_fraction_6h,
            AVG(CASE WHEN p_sog < 2.0 THEN 1.0 ELSE 0.0 END) AS slow_fraction_6h,
            -- COG variability
            COALESCE(STDDEV_SAMP(p_cog), 0.0) AS cog_std_6h,
            -- SOG range (max - min)
            MAX(p_sog) - MIN(p_sog) AS sog_range_6h,
            -- Heading stability
            COALESCE(STDDEV_SAMP(CASE WHEN p_heading < 360 THEN p_heading END), 0.0) AS heading_std_6h,
            -- Count of positions in window
            COUNT(*) AS n_positions_6h,
            -- Time span of positions in window (hours)
            (EXTRACT(EPOCH FROM MAX(p_ts)) - EXTRACT(EPOCH FROM MIN(p_ts))) / 3600.0 AS window_span_h,
            -- First SOG in window
            FIRST(p_sog ORDER BY p_ts ASC) AS sog_6h_ago,
            -- Median SOG in window
            MEDIAN(p_sog) AS median_sog_6h,
            -- Number of significant COG changes
            COUNT(*) FILTER (WHERE prev_cog IS NOT NULL
                             AND ABS(p_cog - prev_cog) > 30
                             AND ABS(p_cog - prev_cog) < 180) AS n_sharp_turns_6h
        FROM traj_ordered
        GROUP BY mmsi, pos_ts, arrival_ts, cur_sog
    """)

    # Add derived features
    con.execute("""
        CREATE TABLE traj_features AS
        SELECT *,
            -- SOG acceleration: current SOG - SOG 6h ago, per hour
            CASE WHEN window_span_h > 0.5
                THEN (cur_sog - sog_6h_ago) / window_span_h
                ELSE 0.0 END AS sog_accel_6h,
            -- Turn frequency: sharp turns per hour
            CASE WHEN window_span_h > 0.5
                THEN n_sharp_turns_6h / window_span_h
                ELSE 0.0 END AS turn_rate_6h
        FROM traj_agg
    """)

    n_agg = con.execute("SELECT count(*) FROM traj_features").fetchone()[0]
    # Stats
    stats = con.execute("""
        SELECT
            ROUND(AVG(stop_fraction_6h) * 100, 1) AS pct_stop,
            ROUND(AVG(cog_std_6h), 1) AS avg_cog_std,
            ROUND(AVG(sog_accel_6h), 2) AS avg_sog_accel,
            ROUND(AVG(turn_rate_6h), 2) AS avg_turn_rate
        FROM traj_features
    """).fetchone()
    print(f"  Aggregated: {n_agg} rows")
    print(f"  Avg stop_fraction_6h={stats[0]}%  cog_std_6h={stats[1]}°  "
          f"sog_accel_6h={stats[2]}kn/h  turn_rate_6h={stats[3]}/h")

    # ── Merge trajectory features back to samples ──
    print("\n── Merging trajectory features ──")
    # Get sample columns (excluding ones that traj_features also has)
    sample_cols = [r[0] for r in con.execute("DESCRIBE samples").fetchall()]
    traj_cols = [r[0] for r in con.execute("DESCRIBE traj_features").fetchall()]
    # Only take traj columns that aren't already in samples (except join keys)
    new_traj_cols = [c for c in traj_cols
                     if c not in sample_cols and c not in ("mmsi",)]
    # Build select list
    select_parts = ["s." + c for c in sample_cols] + ["t." + c for c in new_traj_cols]
    col_str = ",\n    ".join(select_parts)
    con.execute(f"""
        CREATE TABLE samples_traj AS
        SELECT {col_str}
        FROM samples s
        LEFT JOIN traj_features t ON s.mmsi = t.mmsi AND s.pos_ts = t.pos_ts
    """)
    n_merged = con.execute("SELECT count(*) FROM samples_traj").fetchone()[0]
    with_traj = con.execute("SELECT count(*) FROM samples_traj WHERE stop_fraction_6h IS NOT NULL").fetchone()[0]
    print(f"  Merged: {n_merged} rows, {with_traj} with trajectory features ({with_traj/n_merged*100:.0f}%)")

    # ── Port features ──
    print("\n── Port-level features ──")
    con.execute(f"""
        CREATE TABLE port_stats AS
        SELECT
            port_lo_code,
            AVG(time_to_arrival_hours) AS port_avg_tta,
            COUNT(*) AS port_sample_count,
            AVG(sog) AS port_avg_sog
        FROM samples
        WHERE port_lo_code != ''
        GROUP BY port_lo_code
    """)
    n_ports = con.execute("SELECT count(*) FROM port_stats").fetchone()[0]

    # Port congestion: arrivals per hour
    con.execute(f"""
        CREATE TABLE port_congestion AS
        SELECT
            port_lo_code,
            COUNT(*) * 1.0 / GREATEST(
                EXTRACT(EPOCH FROM MAX(arrival_ts) - MIN(arrival_ts)) / 3600.0, 1.0
            ) AS port_arrival_rate_per_hour
        FROM read_parquet('{ARRIVALS}')
        WHERE port_lo_code != ''
        GROUP BY port_lo_code
    """)

    con.execute("""
        CREATE TABLE samples_port AS
        SELECT s.*,
            COALESCE(ps.port_avg_tta, (SELECT AVG(port_avg_tta) FROM port_stats)) AS port_avg_tta,
            COALESCE(ps.port_sample_count, 1) AS port_sample_count,
            COALESCE(ps.port_avg_sog, (SELECT AVG(port_avg_sog) FROM port_stats)) AS port_avg_sog,
            COALESCE(pc.port_arrival_rate_per_hour, 0.0) AS port_arrival_rate_per_hour
        FROM samples_traj s
        LEFT JOIN port_stats ps ON s.port_lo_code = ps.port_lo_code
        LEFT JOIN port_congestion pc ON s.port_lo_code = pc.port_lo_code
    """)
    print(f"  {n_ports} ports with stats")

    # ── MMSI features ──
    print("\n── MMSI-level features ──")
    con.execute("""
        CREATE TABLE mmsi_stats AS
        SELECT
            mmsi,
            STDDEV_SAMP(sog) AS mmsi_sog_std,
            COUNT(*) AS mmsi_sample_count,
            AVG(time_to_arrival_hours) AS mmsi_avg_tta,
            MEDIAN(sog) AS mmsi_median_sog,
            CASE WHEN AVG(sog) > 0 THEN STDDEV_SAMP(sog) / AVG(sog) ELSE 0 END AS mmsi_sog_cv
        FROM samples
        GROUP BY mmsi
    """)
    n_mmsi = con.execute("SELECT count(*) FROM mmsi_stats").fetchone()[0]

    con.execute("""
        CREATE TABLE samples_mmsi AS
        SELECT s.*,
            COALESCE(ms.mmsi_sog_std, 0.0) AS mmsi_sog_std,
            COALESCE(ms.mmsi_sample_count, 1) AS mmsi_sample_count,
            COALESCE(ms.mmsi_avg_tta, s.time_to_arrival_hours) AS mmsi_avg_tta,
            COALESCE(ms.mmsi_median_sog, s.sog) AS mmsi_median_sog,
            COALESCE(ms.mmsi_sog_cv, 0.0) AS mmsi_sog_cv
        FROM samples_port s
        LEFT JOIN mmsi_stats ms ON s.mmsi = ms.mmsi
    """)
    print(f"  {n_mmsi} MMSIs with stats")

    # ── Physics baselines ──
    print("\n── Physics baseline features ──")
    con.execute("""
        CREATE TABLE samples_phys AS
        SELECT *,
            -- eta_phys_v1: uses smoothed 6h speed (which column is available)
            ROUND(dist_to_dest_km / GREATEST(COALESCE(avg_sog_6h, sog), 0.5), 2) AS eta_phys_6h,
            -- eta_phys_v2: penalizes inefficient approach
            ROUND(dist_to_dest_km / GREATEST(
                COALESCE(avg_sog_6h, sog) * GREATEST(COALESCE(approach_efficiency, 0.3), 0.1), 0.5
            ), 2) AS eta_phys_corrected,
            -- eta_phys_v3: closing speed based
            CASE WHEN COALESCE(closing_speed_kmh, 0) > 0.1
                THEN ROUND(dist_to_dest_km / closing_speed_kmh, 2)
                ELSE ROUND(dist_to_dest_km / GREATEST(sog, 1.0), 2)
            END AS eta_phys_closing,
            -- SOG vs vessel typical speed
            sog / GREATEST(COALESCE(mmsi_median_sog, sog), 0.5) AS sog_vs_mmsi_typical
        FROM samples_mmsi
    """)

    # ── Add draught from ShipStaticData ──
    print("\n── Adding draught from ShipStaticData ──")
    try:
        remote = connect_catalog()
        draught_file = DATA_DIR / "_ship_static_draught.parquet"
        print("  Exporting ShipStaticData with draught ...")
        remote.execute(f"""
            COPY (
                SELECT DISTINCT ON (mmsi, ts)
                    mmsi, ts AS static_ts, max_static_draught
                FROM ais.messages
                WHERE message_type = 'ShipStaticData'
                  AND max_static_draught IS NOT NULL
                  AND max_static_draught > 0
                  AND max_static_draught < 30
                ORDER BY mmsi, ts
            ) TO '{draught_file}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        n_draught = remote.execute(f"SELECT count(*) FROM read_parquet('{draught_file}')").fetchone()[0]
        remote.close()
        print(f"  {n_draught} ShipStaticData records with draught")

        # Join: for each sample, get most recent draught before pos_ts
        con.execute(f"""
            CREATE TABLE samples_draught AS
            SELECT DISTINCT ON (s.mmsi, s.pos_ts)
                s.*,
                sd.max_static_draught
            FROM samples_phys s
            LEFT JOIN read_parquet('{draught_file}') sd
                ON s.mmsi = sd.mmsi AND sd.static_ts <= s.pos_ts
            ORDER BY s.mmsi, s.pos_ts, sd.static_ts DESC
        """)
        n_with_draught = con.execute("SELECT count(*) FROM samples_draught WHERE max_static_draught IS NOT NULL").fetchone()[0]
        print(f"  {n_with_draught}/{n} samples have draught ({n_with_draught/n*100:.1f}%)")

        # Fill missing draught
        con.execute("""
            CREATE TABLE samples_draught_filled AS
            SELECT *,
                COALESCE(max_static_draught,
                    MEDIAN(max_static_draught) OVER (PARTITION BY ship_type),
                    MEDIAN(max_static_draught) OVER ()
                ) AS draught_filled
            FROM samples_draught
        """)
        current_table = "samples_draught_filled"
    except Exception as e:
        print(f"  Warning: draught fetch failed ({e}), continuing without draught")
        con.execute("ALTER TABLE samples_phys ADD COLUMN draught_filled DOUBLE DEFAULT 0.0")
        current_table = "samples_phys"

    # ── Final dataset ──
    print("\n── Building final dataset ──")
    con.execute(f"""
        CREATE TABLE dataset_v7 AS
        SELECT *,
            SIN(2 * PI() * hour_of_day / 24.0) AS hour_sin,
            COS(2 * PI() * hour_of_day / 24.0) AS hour_cos,
            SIN(2 * PI() * day_of_week / 7.0) AS dow_sin,
            COS(2 * PI() * day_of_week / 7.0) AS dow_cos,
            -- Target variants
            time_to_arrival_hours / GREATEST(eta_naive_h, 0.05) AS tta_ratio,
            LN(1.0 + time_to_arrival_hours) AS log_tta,
            LN(1.0 + time_to_arrival_hours / GREATEST(eta_naive_h, 0.05)) AS log_tta_ratio
        FROM {current_table}
        WHERE dist_to_dest_km IS NOT NULL
          AND dist_to_dest_km > 0
          AND dist_to_dest_km < 20000
          AND time_to_arrival_hours > 0
          AND time_to_arrival_hours < 200
          AND sog > 0
          AND sog < 50
    """)

    n_final = con.execute("SELECT count(*) FROM dataset_v7").fetchone()[0]
    cols = con.execute("DESCRIBE dataset_v7").fetchdf()
    print(f"  {n_final} rows × {len(cols)} columns")

    # ── Export ──
    con.execute(f"""
        COPY (SELECT * FROM dataset_v7 ORDER BY mmsi, pos_ts)
        TO '{OUTPUT}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    print(f"\n✓ Exported to {OUTPUT}")

    # Stats
    stats = con.execute("""
        SELECT
            ROUND(AVG(time_to_arrival_hours), 1) AS avg_tta,
            ROUND(AVG(stop_fraction_6h) * 100, 1) AS pct_stop,
            ROUND(AVG(cog_std_6h), 1) AS avg_cog_std,
            ROUND(AVG(COALESCE(draught_filled, 0)), 2) AS avg_draught
        FROM dataset_v7
    """).fetchone()
    print(f"  avg_tta={stats[0]}h  stop_6h={stats[1]}%  cog_std_6h={stats[2]}°  draught={stats[3]}m")

    print(f"\nNew columns: stop_fraction_6h, slow_fraction_6h, cog_std_6h, sog_range_6h, "
          f"heading_std_6h, sog_accel_6h, turn_rate_6h, port_avg_tta, port_arrival_rate_per_hour, "
          f"mmsi_sog_std, mmsi_sog_cv, eta_phys_*, draught_filled, hour_sin/cos, dow_sin/cos")

    con.close()
    return n_final


if __name__ == "__main__":
    build()
