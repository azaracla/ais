"""Build empirical route features from 71K arrivals + 197K samples.

Key insight: the v8 model plateaus at ~10h MAE because haversine distance is
ambiguous — a vessel at 500km can be 20h or 50h from arrival depending on route.

Solution: compute "typical TTA from a given distance to a given port" from
historical data. This lookup table directly addresses the ambiguity.

Features added:
  - port_dist_typical_tta: median TTA for (port, distance_bucket)
  - port_dist_sample_count: number of historical samples for this bucket
  - port_dist_tta_std: std of TTA for this bucket (uncertainty proxy)
  - mmsi_port_median_tta: median TTA for (mmsi, port) pair (vessel familiar with port)
  - mmsi_port_visit_count: number of visits by this vessel to this port
  - port_typical_sog: median SOG for vessels approaching this port
  - route_efficiency: dist / (typical_sog * typical_tta) — how direct the route is

Usage:
  uv run python ml/build_route_features.py              # full build
  uv run python ml/build_route_features.py --sample 500   # test
"""

import sys
import duckdb
import numpy as np
import polars as pl
from pathlib import Path

from utils import DATA_DIR

DATASET_V7 = DATA_DIR / "dataset_v7.parquet"
ARRIVALS = DATA_DIR / "arrivals.parquet"
POSITIONS = DATA_DIR / "positions_filtered.parquet"
PORTS = DATA_DIR / "ports.parquet"
OUTPUT = DATA_DIR / "dataset_v9.parquet"

DISTANCE_BUCKETS = [0, 10, 25, 50, 100, 200, 350, 500, 750, 1000, 2000, 5000, 20000]
MIN_SAMPLES_PER_BUCKET = 5

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
    print("Building Empirical Route Features")
    print("=" * 70)

    con = duckdb.connect()

    # ── Load dataset ──
    print("\n── Loading dataset ──")
    con.execute(f"CREATE TABLE samples AS SELECT * FROM read_parquet('{DATASET_V7}')")
    n_all = con.execute("SELECT count(*) FROM samples").fetchone()[0]

    if SAMPLE_N:
        mmsis = con.execute("SELECT DISTINCT mmsi FROM samples USING SAMPLE 200").fetchall()
        mmsi_list = ",".join(str(r[0]) for r in mmsis)
        con.execute(f"CREATE TABLE s AS SELECT * FROM samples WHERE mmsi IN ({mmsi_list})")
        con.execute("DROP TABLE samples; ALTER TABLE s RENAME TO samples")
    n = con.execute("SELECT count(*) FROM samples").fetchone()[0]
    print(f"  {n} samples")

    # ── Port-to-port empirical travel times from arrivals ──
    # For each arrival, compute the journey TTA from positions data
    # (time from first position in our window to arrival)
    print("\n── Computing port-level empirical TTAs from arrivals ──")
    con.execute(f"""
        CREATE TABLE arrival_tta AS
        SELECT
            a.mmsi,
            a.port_lo_code,
            a.arrival_ts,
            a.port_lat,
            a.port_lon,
            -- First position timestamp for this MMSI before arrival (within 10 days)
            MIN(p.ts) AS first_ts,
            -- Time from first position to arrival (hours)
            EXTRACT(EPOCH FROM a.arrival_ts - MIN(p.ts)) / 3600.0 AS journey_tta_h,
            -- Approx origin lat/lon
            FIRST(p.lat ORDER BY p.ts ASC) AS origin_lat,
            FIRST(p.lon ORDER BY p.ts ASC) AS origin_lon
        FROM read_parquet('{ARRIVALS}') a
        LEFT JOIN read_parquet('{POSITIONS}') p
            ON a.mmsi = p.mmsi
            AND p.ts >= a.arrival_ts - INTERVAL '10' DAY
            AND p.ts <= a.arrival_ts
        WHERE a.port_lo_code IS NOT NULL
          AND a.port_lo_code != ''
        GROUP BY a.mmsi, a.port_lo_code, a.arrival_ts, a.port_lat, a.port_lon
    """)
    n_arrivals = con.execute("SELECT count(*) FROM arrival_tta").fetchone()[0]
    # Filter out arrivals with < 1h journey (no position data before arrival)
    n_valid = con.execute(
        "SELECT count(*) FROM arrival_tta WHERE journey_tta_h >= 1.0"
    ).fetchone()[0]
    print(f"  {n_arrivals} arrivals, {n_valid} with journey data (≥1h)")

    # ── Feature 1: Port-distance typical TTA ──
    # Bucketize distance and compute median TTA per (port, distance_bucket)
    print("\n── Computing port-distance typical TTA ──")
    when_clauses = "\n".join(
        f"        WHEN dist_to_dest_km < {DISTANCE_BUCKETS[i+1]} THEN {i}"
        for i in range(len(DISTANCE_BUCKETS) - 1)
    )
    bucket_expr = f"CASE\n{when_clauses}\n        ELSE {len(DISTANCE_BUCKETS) - 1}\n    END"

    con.execute(f"""
        CREATE TABLE port_dist_stats AS
        SELECT
            port_lo_code,
            {bucket_expr} AS dist_bucket,
            COUNT(*) AS sample_count,
            MEDIAN(time_to_arrival_hours) AS median_tta,
            AVG(time_to_arrival_hours) AS avg_tta,
            STDDEV_SAMP(time_to_arrival_hours) AS std_tta,
            MEDIAN(sog) AS median_sog,
            AVG(approach_efficiency) AS avg_approach_efficiency
        FROM samples
        WHERE port_lo_code IS NOT NULL AND port_lo_code != ''
        GROUP BY port_lo_code, dist_bucket
        HAVING COUNT(*) >= {MIN_SAMPLES_PER_BUCKET}
        ORDER BY port_lo_code, dist_bucket
    """)
    n_buckets = con.execute("SELECT count(*) FROM port_dist_stats").fetchone()[0]
    print(f"  {n_buckets} port-distance buckets (≥{MIN_SAMPLES_PER_BUCKET} samples)")

    # ── Feature 2: MMSI-port familiarity ──
    print("\n── Computing MMSI-port familiarity ──")
    con.execute("""
        CREATE TABLE mmsi_port_stats AS
        SELECT
            mmsi,
            port_lo_code,
            COUNT(*) AS visit_count,
            MEDIAN(time_to_arrival_hours) AS mmsi_port_median_tta,
            AVG(time_to_arrival_hours) AS mmsi_port_avg_tta,
            AVG(sog) AS mmsi_port_avg_sog,
            AVG(dist_to_dest_km) AS mmsi_port_avg_dist
        FROM samples
        WHERE port_lo_code IS NOT NULL AND port_lo_code != ''
        GROUP BY mmsi, port_lo_code
        HAVING COUNT(*) >= 3
    """)
    n_mmsi_port = con.execute("SELECT count(*) FROM mmsi_port_stats").fetchone()[0]
    print(f"  {n_mmsi_port} MMSI-port pairs (≥3 samples)")

    # ── Feature 3: Global distance-TTA lookup (port-independent) ──
    print("\n── Computing global distance-TTA lookup ──")
    con.execute(f"""
        CREATE TABLE global_dist_stats AS
        SELECT
            {bucket_expr} AS dist_bucket,
            MEDIAN(time_to_arrival_hours) AS global_median_tta,
            AVG(time_to_arrival_hours) AS global_avg_tta,
            STDDEV_SAMP(time_to_arrival_hours) AS global_std_tta,
            COUNT(*) AS global_sample_count
        FROM samples
        GROUP BY dist_bucket
        ORDER BY dist_bucket
    """)
    n_global = con.execute("SELECT count(*) FROM global_dist_stats").fetchone()[0]
    print(f"  {n_global} global distance buckets")

    # ── Join features to samples ──
    print("\n── Joining features to samples ──")

    # Build distance bucket column on samples
    sample_cols = [r[0] for r in con.execute("DESCRIBE samples").fetchall()]

    con.execute(f"""
        CREATE TABLE samples_bucketed AS
        SELECT *,
            {bucket_expr} AS dist_bucket
        FROM samples
    """)

    # Join port-dist stats
    con.execute("""
        CREATE TABLE samples_enriched AS
        SELECT s.*,
            pds.median_tta AS port_dist_typical_tta,
            pds.sample_count AS port_dist_sample_count,
            pds.std_tta AS port_dist_tta_std,
            pds.median_sog AS port_dist_median_sog,
            pds.avg_approach_efficiency AS port_dist_avg_eff,
            -- MMSI-port features
            mps.visit_count AS mmsi_port_visit_count,
            mps.mmsi_port_median_tta,
            mps.mmsi_port_avg_sog AS mmsi_port_typical_sog,
            mps.mmsi_port_avg_dist AS mmsi_port_typical_dist,
            -- Global distance stats
            gds.global_median_tta AS dist_typical_tta,
            gds.global_std_tta AS dist_tta_std
        FROM samples_bucketed s
        LEFT JOIN port_dist_stats pds
            ON s.port_lo_code = pds.port_lo_code
            AND s.dist_bucket = pds.dist_bucket
        LEFT JOIN mmsi_port_stats mps
            ON s.mmsi = mps.mmsi
            AND s.port_lo_code = mps.port_lo_code
        LEFT JOIN global_dist_stats gds
            ON s.dist_bucket = gds.dist_bucket
    """)

    # ── Compute derived features ──
    print("── Computing derived route features ──")
    con.execute("""
        CREATE TABLE samples_final AS
        SELECT *,
            -- Route efficiency: how much better is port-specific TTA vs global?
            CASE WHEN port_dist_typical_tta IS NOT NULL AND dist_typical_tta > 0
                THEN dist_typical_tta / NULLIF(port_dist_typical_tta, 0)
                ELSE 1.0 END AS port_route_efficiency,
            -- MMSI-port familiarity: how well does this vessel know this port?
            CASE WHEN mmsi_port_median_tta IS NOT NULL AND eta_naive_h > 0
                THEN eta_naive_h / NULLIF(mmsi_port_median_tta, 0)
                ELSE 1.0 END AS mmsi_port_tta_ratio,
            -- Port congestion proxy: how much slower is current approach vs typical?
            CASE WHEN port_dist_median_sog IS NOT NULL AND port_dist_median_sog > 0
                THEN sog / port_dist_median_sog
                ELSE 1.0 END AS port_congestion_ratio,
            -- Uncertainty: CV of TTA for this port-distance bucket
            CASE WHEN port_dist_typical_tta IS NOT NULL AND port_dist_typical_tta > 0
                THEN port_dist_tta_std / port_dist_typical_tta
                ELSE 1.0 END AS port_dist_tta_cv
        FROM samples_enriched
    """)

    # ── Export ──
    print("\n── Exporting ──")
    n_final = con.execute("SELECT count(*) FROM samples_final").fetchone()[0]

    # Count coverage
    n_with_port_dist = con.execute(
        "SELECT count(*) FROM samples_final WHERE port_dist_typical_tta IS NOT NULL"
    ).fetchone()[0]
    n_with_mmsi_port = con.execute(
        "SELECT count(*) FROM samples_final WHERE mmsi_port_median_tta IS NOT NULL"
    ).fetchone()[0]
    n_with_dist = con.execute(
        "SELECT count(*) FROM samples_final WHERE dist_typical_tta IS NOT NULL"
    ).fetchone()[0]

    print(f"  Samples: {n_final}")
    print(f"  With port-dist TTA: {n_with_port_dist} ({n_with_port_dist/n_final*100:.1f}%)")
    print(f"  With MMSI-port TTA: {n_with_mmsi_port} ({n_with_mmsi_port/n_final*100:.1f}%)")
    print(f"  With global dist TTA: {n_with_dist} ({n_with_dist/n_final*100:.1f}%)")

    # Compare: naive eta vs port-dist eta
    comp = con.execute("""
        SELECT
            AVG(ABS(time_to_arrival_hours - eta_naive_h)) AS naive_mae,
            AVG(ABS(time_to_arrival_hours - COALESCE(port_dist_typical_tta, dist_typical_tta, eta_naive_h))) AS route_mae
        FROM samples_final
        WHERE port_dist_typical_tta IS NOT NULL OR dist_typical_tta IS NOT NULL
    """).fetchone()
    print(f"  Naive MAE: {comp[0]:.1f}h")
    print(f"  Route-based MAE: {comp[1]:.1f}h")
    print(f"  Improvement: {(1-comp[1]/comp[0])*100:.1f}%")

    # Export
    final_cols = [c[0] for c in con.execute("DESCRIBE samples_final").fetchall()]
    new_cols = [c for c in final_cols if c not in sample_cols]
    print(f"\n  New features added ({len(new_cols)}):")
    for c in new_cols:
        print(f"    - {c}")

    # Write to Parquet via polars
    df = pl.from_arrow(con.execute("SELECT * FROM samples_final").fetch_arrow_table())
    df.write_parquet(OUTPUT, compression="zstd")
    con.close()

    print(f"\n✓ Saved to {OUTPUT}")
    print(f"  Columns: {df.width} (was {len(sample_cols)})")


if __name__ == "__main__":
    build()
