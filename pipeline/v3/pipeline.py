#!/usr/bin/env python3
"""
DuckLake v3 Pipeline — Fast local transform, DuckLake catalog management.

Architecture (CI-safe, no historical data needed locally):
  1. DOWNLOAD   — ThreadPoolExecutor: NDJSON.zst S3 → local
  2. CONSOLIDATE — DuckDB local: read_ndjson → dedup → COPY TO silver.parquet
  3. DERIVE     — DuckDB local: read silver.parquet → COPY TO gold tables
  4. UPLOAD     — ThreadPoolExecutor: Parquet locaux → S3 public-read
  5. CATALOG    — DuckLake (S3 DATA_PATH): register files, upload catalog

Usage:
  python pipeline/v3/pipeline.py --date YYYY-MM-DD
  python pipeline/v3/pipeline.py --from YYYY-MM-DD --to YYYY-MM-DD
  python pipeline/v3/pipeline.py --date YYYY-MM-DD --force
  python pipeline/v3/pipeline.py --tables port_calls,port_congestion
"""

import argparse
import os
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta

from config import work_dir, output_dir, s3_client
from download import list_raw_files, download_files
from transform import local_transform
from upload import upload_output_files
from catalog import update_catalog

VALID_TABLES = {
    'messages', 'vessels_positions', 'vessel_tracks',
    'base_stations', 'aids_to_navigation', 'vessels',
    'port_calls', 'port_congestion',
}

STATS_TABLES = [
    'messages', 'vessels_positions', 'vessel_tracks',
    'base_stations', 'aids_to_navigation', 'vessels',
    'port_calls', 'port_congestion',
]


def process_date(target_date, force=False, no_download=False, tables=None):
    ts = target_date.strftime('%Y-%m-%d')
    print(f"\n{'═' * 70}")
    print(f"📅 {ts}")
    if tables:
        print(f"   🎯 Tables: {', '.join(sorted(tables))}")
    print(f"{'═' * 70}")

    s3 = s3_client()
    wd = work_dir(target_date)
    od = output_dir(target_date)

    # 1. Download raw files (only if messages is requested)
    all_tables = tables is None
    need_messages = all_tables or 'messages' in tables

    if need_messages:
        keys = list_raw_files(s3, target_date)
        print(f"🔍 {len(keys)} fichiers raw trouvés")
        if not keys:
            print("⚠️ Aucune donnée pour cette date.")
            return

        os.makedirs(wd, exist_ok=True)
        existing = [f for f in os.listdir(wd) if f.endswith('.ndjson.zst')]
        expected = len(keys)
        if no_download or len(existing) == expected:
            if no_download:
                print(f"📂 --no-download: {len(existing)} fichiers dans {wd}, skip")
            else:
                print(f"📂 Cache: {len(existing)}/{expected} fichiers déjà là, "
                      f"skip download")
            downloaded, failed = len(existing), 0
        else:
            for f in existing:
                os.remove(os.path.join(wd, f))
            t0 = time.time()
            print(f"📥 Download {expected} fichiers NDJSON.zst → {wd}...")
            downloaded, failed = download_files(s3, keys, wd)
            print(f"   ✅ {downloaded} ok, {failed} fail ({time.time() - t0:.1f}s)")

        if downloaded == 0:
            print("❌ Aucun fichier téléchargé.")
            return

    # 2-3. Local transform
    t_start = time.time()
    stats = local_transform(target_date, wd, od, tables)
    print(f"   ⏱️  Transform locale: {time.time() - t_start:.1f}s")

    # 4. Upload
    t2 = time.time()
    uploaded_files = upload_output_files(s3, od)
    print(f"   ⏱️  Upload: {time.time() - t2:.1f}s")

    # 5. Catalog
    t3 = time.time()
    update_catalog(s3, uploaded_files, target_date, force, od, tables)
    print(f"   ⏱️  Catalogue: {time.time() - t3:.1f}s")

    # Cleanup output (keep work dir for caching)
    shutil.rmtree(od, ignore_errors=True)

    print(f"\n✅ {ts} terminé en {time.time() - t_start:.1f}s")
    for t in STATS_TABLES:
        if t in stats:
            print(f"   {t:<20} {stats[t]:,}")


def date_range(start, end):
    days = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    return days


def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"❌ Format invalide: '{s}'. Utilisez YYYY-MM-DD.")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DuckLake v3 Pipeline")
    parser.add_argument("--date", type=str, default=None,
                        help="Date YYYY-MM-DD (défaut: hier)")
    parser.add_argument("--from", dest="start_date", type=str, default=None,
                        help="Date début YYYY-MM-DD (inclus)")
    parser.add_argument("--to", dest="end_date", type=str, default=None,
                        help="Date fin YYYY-MM-DD (inclus)")
    parser.add_argument("--force", action="store_true",
                        help="Nettoie la partition avant republication")
    parser.add_argument("--no-download", action="store_true",
                        help="Skip download, use fichiers déjà dans work/")
    parser.add_argument("--tables", type=str, default=None,
                        help="Tables à générer, séparées par des virgules "
                             f"({', '.join(sorted(VALID_TABLES))}). "
                             "Défaut: toutes.")
    args = parser.parse_args()

    tables = set(t.strip() for t in args.tables.split(',')) \
        if args.tables else None
    if tables:
        unknown = tables - VALID_TABLES
        if unknown:
            print(f"❌ Tables inconnues: {', '.join(sorted(unknown))}")
            print(f"   Valides: {', '.join(sorted(VALID_TABLES))}")
            sys.exit(1)

    if args.start_date and args.end_date:
        dates = date_range(parse_date(args.start_date),
                           parse_date(args.end_date))
    elif args.start_date:
        print("❌ --from nécessite --to")
        sys.exit(1)
    elif args.end_date:
        print("❌ --to nécessite --from")
        sys.exit(1)
    elif args.date:
        dates = [parse_date(args.date)]
    else:
        dates = [datetime.now(timezone.utc) - timedelta(days=1)]

    for d in dates:
        process_date(d, force=args.force, no_download=args.no_download,
                     tables=tables)
