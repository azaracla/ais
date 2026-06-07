"""Build maritime route distance features using searoute.

Computes route distance (shortest sea path) vs haversine (straight line)
for each sample, creating a correction factor per maritime corridor.

Strategy:
  1. Extract unique (rounded_lat, rounded_lon, port_lo_code) combos
  2. Compute searoute for each → route distance in km
  3. Compute route_vs_haversine ratio
  4. Merge back to samples and add as features

Performance: searoute is ~0.001s/call after warmup.
For 197K samples with ~10K unique combos → ~10 seconds.
"""

import sys
import duckdb
import numpy as np
import polars as pl
import searoute as sr
from pathlib import Path
from collections import defaultdict

from utils import DATA_DIR

DATASET_V7 = DATA_DIR / "dataset_v7.parquet"
ARRIVALS = DATA_DIR / "arrivals.parquet"
OUTPUT = DATA_DIR / "dataset_v8.parquet"

# Rounding precision for position grid (degrees)
# 0.5° ≈ 55km at equator → ~3K unique grid cells
ROUND_POS = 0.5


def build():
    print("=" * 70)
    print("Building Route Distance Features (searoute)")
    print("=" * 70)

    # ── Load dataset and port coordinates ──
    con = duckdb.connect()
    con.execute(f"CREATE TABLE ds AS SELECT * FROM read_parquet('{DATASET_V7}')")
    n = con.execute("SELECT count(*) FROM ds").fetchone()[0]
    print(f"  Loaded {n} samples")

    # Get port coordinates from arrivals
    con.execute(f"""
        CREATE TABLE ports AS
        SELECT DISTINCT port_lo_code, port_lat, port_lon
        FROM read_parquet('{ARRIVALS}')
        WHERE port_lat != 0 AND port_lon != 0 AND port_lo_code != ''
    """)
    n_ports = con.execute("SELECT count(*) FROM ports").fetchone()[0]
    print(f"  {n_ports} unique ports with coordinates")

    # Join port coords, and reconstruct vessel position from arrival coords
    con.execute("""
        CREATE TABLE ds_ports AS
        SELECT d.*, p.port_lat, p.port_lon
        FROM ds d
        LEFT JOIN ports p ON d.port_lo_code = p.port_lo_code
    """)

    # We need vessel lat/lon. The dataset has dist_to_dest_km but not original pos.
    # We can get vessel position from the _samples.parquet (which has pos_lat, pos_lon)
    samples_path = DATA_DIR / "_samples.parquet"
    if samples_path.exists():
        con.execute(f"""
            CREATE TABLE samples_pos AS
            SELECT mmsi, pos_ts, pos_lat, pos_lon
            FROM read_parquet('{samples_path}')
        """)
        con.execute("""
            CREATE TABLE ds_with_pos AS
            SELECT d.*, s.pos_lat, s.pos_lon
            FROM ds_ports d
            LEFT JOIN samples_pos s ON d.mmsi = s.mmsi AND d.pos_ts = s.pos_ts
        """)
    else:
        print("  WARNING: _samples.parquet not found, can't get vessel positions")
        con.execute("ALTER TABLE ds_ports ADD COLUMN pos_lat DOUBLE DEFAULT 0")
        con.execute("ALTER TABLE ds_ports ADD COLUMN pos_lon DOUBLE DEFAULT 0")
        con.execute("ALTER TABLE ds_ports RENAME TO ds_with_pos")

    # Count samples with position info
    n_pos = con.execute(
        "SELECT count(*) FROM ds_with_pos WHERE pos_lat IS NOT NULL AND port_lat IS NOT NULL"
    ).fetchone()[0]
    print(f"  {n_pos} samples have both vessel and port positions")

    # ── Compute unique corridors ──
    print(f"\n── Computing unique corridors (grid {ROUND_POS}°) ──")
    con.execute(f"""
        CREATE TABLE corridors AS
        SELECT
            ROUND(pos_lat / {ROUND_POS}) * {ROUND_POS} AS grid_lat,
            ROUND(pos_lon / {ROUND_POS}) * {ROUND_POS} AS grid_lon,
            port_lo_code,
            port_lat, port_lon,
            COUNT(*) AS n_samples
        FROM ds_with_pos
        WHERE pos_lat IS NOT NULL AND port_lat IS NOT NULL
          AND dist_to_dest_km > 10
        GROUP BY 1, 2, 3, 4, 5
        ORDER BY n_samples DESC
    """)
    corridors = con.execute("SELECT * FROM corridors").fetchall()
    n_corr = len(corridors)
    print(f"  {n_corr} unique corridors")

    # ── Compute searoute for each corridor ──
    print(f"\n── Computing searoute distances (this may take a few minutes) ──")
    route_distances = {}
    errors = 0

    for i, (grid_lat, grid_lon, port_code, port_lat, port_lon, n_s) in enumerate(corridors):
        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{n_corr} corridors computed ({n_corr-i-1} remaining)")
        try:
            route = sr.searoute(
                [grid_lon, grid_lat],
                [port_lon, port_lat],
                units="naut",
                speed_knot=10,
            )
            dist_nm = route.properties.get("length", 0)
            dist_km = dist_nm * 1.852
            route_distances[(grid_lat, grid_lon, port_code)] = dist_km
        except Exception:
            errors += 1
            route_distances[(grid_lat, grid_lon, port_code)] = -1.0

    print(f"  {n_corr} corridors, {errors} errors ({errors/n_corr*100:.1f}%)")

    # ── Build lookup table ──
    routes_df = pl.DataFrame([
        {"grid_lat": k[0], "grid_lon": k[1], "port_code": k[2], "route_dist_km": v}
        for k, v in route_distances.items()
    ])
    routes_path = DATA_DIR / "_route_distances.parquet"
    routes_df.write_parquet(routes_path)
    print(f"  Saved route distances to {routes_path}")

    # ── Add route features to dataset ──
    print(f"\n── Adding route features ──")
    con.execute(f"CREATE TABLE routes AS SELECT * FROM read_parquet('{routes_path}')")
    con.execute(f"""
        CREATE TABLE ds_routes AS
        SELECT d.*,
            ROUND(d.pos_lat / {ROUND_POS}) * {ROUND_POS} AS _grid_lat,
            ROUND(d.pos_lon / {ROUND_POS}) * {ROUND_POS} AS _grid_lon
        FROM ds_with_pos d
    """)
    con.execute("""
        CREATE TABLE ds_enriched AS
        SELECT d.*,
            r.route_dist_km,
            CASE
                WHEN r.route_dist_km > 0 AND d.dist_to_dest_km > 0
                THEN ROUND(r.route_dist_km / d.dist_to_dest_km, 3)
                WHEN r.route_dist_km > 0 AND d.dist_to_dest_km <= 0
                THEN 1.0
                ELSE NULL
            END AS route_vs_haversine,
            CASE
                WHEN r.route_dist_km > 0 AND d.avg_sog_6h > 0.5
                THEN ROUND(r.route_dist_km / d.avg_sog_6h, 2)
                WHEN r.route_dist_km > 0
                THEN ROUND(r.route_dist_km / GREATEST(d.sog, 0.5), 2)
                ELSE NULL
            END AS eta_route
        FROM ds_routes d
        LEFT JOIN routes r
            ON d._grid_lat = r.grid_lat
            AND d._grid_lon = r.grid_lon
            AND d.port_lo_code = r.port_code
    """)

    n_routes = con.execute(
        "SELECT count(*) FROM ds_enriched WHERE route_dist_km IS NOT NULL"
    ).fetchone()[0]
    print(f"  {n_routes}/{n} samples have route distance ({n_routes/n*100:.1f}%)")

    # Stats
    stats = con.execute("""
        SELECT
            ROUND(AVG(route_vs_haversine), 3) AS avg_ratio,
            ROUND(STDDEV_SAMP(route_vs_haversine), 3) AS std_ratio,
            MIN(route_vs_haversine) AS min_ratio,
            MAX(route_vs_haversine) AS max_ratio
        FROM ds_enriched
        WHERE route_vs_haversine IS NOT NULL AND route_vs_haversine > 0
    """).fetchone()
    print(f"  route_vs_haversine: mean={stats[0]} std={stats[1]} min={stats[2]} max={stats[3]}")
    print(f"  (Ratio > 1.0 means route is longer than straight line — expected for maritime paths)")

    # ── Export ──
    # Clean up temp columns
    con.execute("""
        CREATE TABLE dataset_v8 AS
        SELECT * EXCLUDE (_grid_lat, _grid_lon, pos_lat, pos_lon, port_lat, port_lon)
        FROM ds_enriched
    """)
    n_final = con.execute("SELECT count(*) FROM dataset_v8").fetchone()[0]

    con.execute(f"""
        COPY (SELECT * FROM dataset_v8 ORDER BY mmsi, pos_ts)
        TO '{OUTPUT}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    print(f"\n✓ Exported {n_final} rows to {OUTPUT}")

    # Feature coverage
    for col in ["route_dist_km", "route_vs_haversine", "eta_route"]:
        non_null = con.execute(f"SELECT count(*) FROM dataset_v8 WHERE {col} IS NOT NULL").fetchone()[0]
        print(f"  {col}: {non_null}/{n_final} non-null ({non_null/n_final*100:.1f}%)")

    con.close()
    return n_final


if __name__ == "__main__":
    # Quick test mode
    if "--quick" in sys.argv:
        ROUND_POS = 2.0  # fewer corridors for quick test
        print("QUICK MODE: ROUND_POS=2.0")

    build()
