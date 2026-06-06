#!/usr/bin/env python3
"""
Benchmark: sort order impact on spatial bbox pruning for vessels_positions.

Generates multiple Parquet files from same data with different ORDER BY,
then compares query time AND row groups read for bbox queries.

Sort strategies:
  temporal    — ORDER BY ts, mmsi           (current, good for time range)
  spatial_lat — ORDER BY lat, mmsi           (prune on lat dimension)
  spatial_latlon — ORDER BY lat, lon, mmsi   (prune on both dimensions)
  spatial_geo — ORDER BY geo_cell, mmsi      (Z-order / grid cell ~0.1°)

Usage:
  python pipeline/v3/test_geo_parquet.py [--date YYYY-MM-DD] [--sample N]
    --date   : date (default: latest in work/)
    --sample : max NDJSON files (default: 0 = all files)
"""

import argparse
import os
import sys
import time
import duckdb
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_BASE_DIR = os.path.join(SCRIPT_DIR, 'work')
TEST_DIR = os.path.join(SCRIPT_DIR, 'test_output')

os.makedirs(TEST_DIR, exist_ok=True)

SORT_STRATEGIES = {
    'temporal':      "ORDER BY ts ASC, mmsi ASC",
    'spatial_lat':   "ORDER BY lat ASC, mmsi ASC",
    'spatial_latlon': "ORDER BY lat ASC, lon ASC, mmsi ASC",
    'spatial_geo':   "ORDER BY geo_cell ASC, mmsi ASC",
}

BBOXES = [
    # name, south, north, west, east
    ("English Channel",      49.5, 51.5,  -5.0,   2.0),
    ("Strait of Gibraltar",  35.5, 36.5,  -6.0,  -5.0),
    ("Singapore Strait",      1.0,  1.5, 103.5, 104.5),
    ("North Sea",            51.0, 56.0,   0.0,   7.0),
    ("West Mediterranean",   35.0, 42.0,  -5.0,  15.0),
    ("Panama Canal",          8.5,  9.5, -80.0, -79.0),
    ("Suez Canal",           29.5, 31.5,  32.0,  33.0),
    ("Malacca Strait",        1.0,  4.0, 100.0, 104.0),
    ("Global view",         -60.0, 70.0,-180.0, 180.0),
]

ROW_GROUP_SIZE = 100000


def find_latest_work_date():
    if not os.path.exists(WORK_BASE_DIR):
        return None
    dirs = sorted([
        d for d in os.listdir(WORK_BASE_DIR)
        if os.path.isdir(os.path.join(WORK_BASE_DIR, d))
    ], reverse=True)
    return dirs[0] if dirs else None


def count_files(work_dir: str, sample: int):
    files = sorted([f for f in os.listdir(work_dir) if f.endswith('.ndjson.zst')])
    return files[:sample] if sample > 0 else files


def consolidate_full(work_dir: str, files: list[str], output_path: str):
    """Consolidate raw NDJSON files into silver Parquet (all position messages)."""
    n = len(files)
    print(f"\n📦 Consolidating {n} NDJSON files → {output_path}")
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'; SET threads=8;")
    try:
        con.execute(f"""
            COPY (
                SELECT
                    message_type,
                    metadata.MMSI::BIGINT AS mmsi,
                    CASE
                        WHEN metadata.time_utc LIKE '% UTC' THEN
                            replace(replace(metadata.time_utc, ' UTC', ''), ' +0000', '')::TIMESTAMPTZ
                        ELSE metadata.time_utc::TIMESTAMPTZ
                    END AS ts,
                    metadata.latitude::DOUBLE AS lat,
                    metadata.longitude::DOUBLE AS lon,
                    message[message_type]['Sog']::DOUBLE AS sog,
                    message[message_type]['Cog']::DOUBLE AS cog,
                    message[message_type]['TrueHeading']::INTEGER AS true_heading,
                    message[message_type]['NavigationalStatus']::INTEGER AS navigational_status,
                    message[message_type]['MessageID']::INTEGER AS message_id,
                    message[message_type]['Valid']::BOOLEAN AS valid,
                FROM read_ndjson('{os.path.join(work_dir, '*.ndjson.zst')}',
                                 ignore_errors = true)
                WHERE metadata.MMSI IS NOT NULL
                  AND message_type IN (
                      'PositionReport', 'ExtendedClassBPositionReport',
                      'StandardClassBPositionReport', 'LongRangeAisBroadcast'
                  )
                  AND metadata.latitude IS NOT NULL
                  AND metadata.longitude IS NOT NULL
            ) TO '{output_path}'
            (FORMAT 'PARQUET', COMPRESSION 'ZSTD', ROW_GROUP_SIZE {ROW_GROUP_SIZE})
        """)
        n_rows = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{output_path}')"
        ).fetchone()[0]
        elapsed = time.time() - t0
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        print(f"   ✅ {n_rows:,} rows, {size_mb:.0f} MB in {elapsed:.1f}s")
        return n_rows
    finally:
        con.close()


def generate_sorted(silver_path: str, sort_sql: str, output_path: str, label: str):
    """Generate vessels_positions Parquet with specific sort order."""
    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'; SET threads=8;")
    try:
        t0 = time.time()
        # Use ts directly — DuckDB auto-converts Parquet INT64 logical TIMESTAMP
        con.execute(f"""
            COPY (
                SELECT
                    mmsi, ts, lat, lon,
                    sog, cog, true_heading, navigational_status,
                    message_id, valid,
                    CAST(year(ts) AS INTEGER) AS year,
                    CAST(month(ts) AS INTEGER) AS month,
                    CAST(day(ts) AS INTEGER) AS day,
                    (FLOOR(lat * 10)::BIGINT * 100000 + FLOOR(lon * 10)::BIGINT) AS geo_cell
                FROM read_parquet('{silver_path}')
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND message_type IN (
                      'PositionReport', 'ExtendedClassBPositionReport',
                      'StandardClassBPositionReport', 'LongRangeAisBroadcast'
                  )
                {sort_sql}
            ) TO '{output_path}'
            (FORMAT 'PARQUET', COMPRESSION 'ZSTD',
             COMPRESSION_LEVEL 3, ROW_GROUP_SIZE {ROW_GROUP_SIZE})
        """)
        n_rows = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{output_path}')"
        ).fetchone()[0]
        elapsed = time.time() - t0
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        n_rg = con.execute(f"""
            SELECT COUNT(DISTINCT row_group_id) FROM parquet_metadata('{output_path}')
        """).fetchone()[0]
        print(f"   ✅ {label:20s}: {n_rows:>10,} rows, {n_rg:>3} RG, {size_mb:>6.0f} MB in {elapsed:.1f}s")
        return n_rows, n_rg, size_mb
    finally:
        con.close()


def benchmark_sort(parquet_path: str, label: str):
    """Run bbox queries, measure time and row groups scanned."""
    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'; SET threads=4;")

    # Total row groups
    total_rg = con.execute(f"""
        SELECT COUNT(DISTINCT row_group_id) FROM parquet_metadata('{parquet_path}')
    """).fetchone()[0]

    results = []
    for name, south, north, west, east in BBOXES:
        # Measure query time
        t0 = time.time()
        n_rows = con.execute(f"""
            SELECT COUNT(*) FROM read_parquet('{parquet_path}')
            WHERE lat BETWEEN {south} AND {north}
              AND lon BETWEEN {west} AND {east}
        """).fetchone()[0]
        elapsed = (time.time() - t0) * 1000

        # Count row groups whose lat/lon min/max overlaps the bbox
        # stats_min_value/stats_max_value are VARCHAR — cast to DOUBLE
        rg_scanned = con.execute(f"""
            WITH lat_rg AS (
                SELECT DISTINCT row_group_id
                FROM parquet_metadata('{parquet_path}')
                WHERE path_in_schema = 'lat'
                  AND stats_min_value IS NOT NULL
                  AND stats_max_value IS NOT NULL
                  AND CAST(stats_min_value AS DOUBLE) <= {north}
                  AND CAST(stats_max_value AS DOUBLE) >= {south}
            ),
            lon_rg AS (
                SELECT DISTINCT row_group_id
                FROM parquet_metadata('{parquet_path}')
                WHERE path_in_schema = 'lon'
                  AND stats_min_value IS NOT NULL
                  AND stats_max_value IS NOT NULL
                  AND CAST(stats_min_value AS DOUBLE) <= {east}
                  AND CAST(stats_max_value AS DOUBLE) >= {west}
            )
            SELECT COUNT(*) FROM lat_rg JOIN lon_rg USING (row_group_id)
        """).fetchone()[0]

        results.append((name, n_rows, elapsed, total_rg, rg_scanned))

    con.close()
    return results


def print_results(all_results: dict):
    """Print comparison table: sort strategy × bbox."""
    strategies = list(all_results.keys())
    bbox_names = [r[0] for r in all_results[strategies[0]]]

    print(f"\n{'='*120}")
    print("RESULTS: Query time (ms) & Row Groups scanned")
    print(f"{'='*120}")

    # Header
    header = f"{'BBox':<24s}"
    for s in strategies:
        header += f" | {s:>25s}"
    print(header)
    print("-" * len(header))

    # For each bbox
    for i, bname in enumerate(bbox_names):
        line = f"{bname:<24s}"
        for s in strategies:
            _, n_rows, elapsed, total_rg, rg_scanned = all_results[s][i]
            pct = rg_scanned / total_rg * 100 if total_rg else 0
            line += f" | {elapsed:>5.0f}ms {rg_scanned:>2d}/{total_rg:<2d}RG({pct:>3.0f}%)"
        print(line)

    # Summary: average time and pruning efficiency
    print(f"\n{'─'*120}")
    print("SUMMARY")
    print(f"{'─'*120}")
    print(f"{'Strategy':<20s} {'Avg time':>8s} {'Min RG%':>8s} {'Max RG%':>8s} {'Avg RG%':>8s} {'File size':>10s}")
    for s in strategies:
        times = [r[2] for r in all_results[s]]
        rg_pcts = [r[4]/r[3]*100 if r[3] else 0 for r in all_results[s]]
        avg_t = sum(times) / len(times)
        file_size = os.path.getsize(
            os.path.join(TEST_DIR, f'vp_{s}_{date_str}.parquet')
        ) / 1024 / 1024
        print(f"{s:<20s} {avg_t:>7.1f}ms {min(rg_pcts):>7.0f}% {max(rg_pcts):>7.0f}% {sum(rg_pcts)/len(rg_pcts):>7.0f}% {file_size:>8.0f} MB")


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark sort order for spatial bbox pruning"
    )
    parser.add_argument("--date", type=str, default=None,
                        help="Date (default: latest in work/)")
    parser.add_argument("--sample", type=int, default=0,
                        help="Max NDJSON files (0=all)")
    args = parser.parse_args()

    global date_str
    date_str = args.date or find_latest_work_date()
    if not date_str:
        print("❌ No work data found.")
        sys.exit(1)

    work_dir = os.path.join(WORK_BASE_DIR, date_str)
    if not os.path.exists(work_dir):
        print(f"❌ Work dir not found: {work_dir}")
        sys.exit(1)

    # Use existing silver from v1 pipeline if available, otherwise consolidate
    v1_silver = os.path.join(
        SCRIPT_DIR, '..',
        f"silver/year={date_str[:4]}/month={date_str[5:7]}/day={date_str[8:10]}",
        "messages_consolidated.parquet"
    )
    v1_silver = os.path.abspath(v1_silver)

    if os.path.exists(v1_silver):
        silver_path = v1_silver
        print(f"{'='*60}")
        print(f"Sort Order Bbox Pruning Benchmark — {date_str}")
        print(f"Silver: {silver_path} ({os.path.getsize(silver_path)/1024/1024:.0f} MB)")
        print(f"Row group size: {ROW_GROUP_SIZE:,}")
        print(f"{'='*60}")
    else:
        files = count_files(work_dir, args.sample)
        print(f"{'='*60}")
        print(f"Sort Order Bbox Pruning Benchmark — {date_str}")
        print(f"Files: {len(files)}, Row group size: {ROW_GROUP_SIZE:,}")
        print(f"{'='*60}")
        silver_path = os.path.join(TEST_DIR, f'silver_full_{date_str}.parquet')
        if os.path.exists(silver_path):
            print(f"\n♻️  Using existing silver: {silver_path} "
                  f"({os.path.getsize(silver_path)/1024/1024:.0f} MB)")
        else:
            consolidate_full(work_dir, files, silver_path)

    # 2. Generate one Parquet per sort strategy
    print(f"\n{'─'*60}")
    print("Generating sorted Parquet files...")
    print(f"{'─'*60}")
    for strategy, sort_sql in SORT_STRATEGIES.items():
        output_path = os.path.join(TEST_DIR, f'vp_{strategy}_{date_str}.parquet')
        if os.path.exists(output_path):
            print(f"   ♻️  {strategy}: exists, skip")
        else:
            generate_sorted(silver_path, sort_sql, output_path, strategy)

    # 3. Benchmark each
    print(f"\n{'─'*60}")
    print("Benchmarking bbox queries...")
    print(f"{'─'*60}")
    all_results = {}
    for strategy in SORT_STRATEGIES:
        parquet_path = os.path.join(TEST_DIR, f'vp_{strategy}_{date_str}.parquet')
        print(f"   🔍 {strategy}...")
        all_results[strategy] = benchmark_sort(parquet_path, strategy)

    # 4. Print comparison
    print_results(all_results)

    print(f"\n✅ Done. Files in {TEST_DIR}/")


if __name__ == "__main__":
    main()
