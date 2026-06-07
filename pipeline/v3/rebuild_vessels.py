#!/usr/bin/env python3
"""
Rebuild the complete vessels table from all silver data in a date range.

Reads ALL silver Parquet files via HTTPS (public bucket) in a single DuckDB query,
extracts static vessel info, deduplicates globally by MMSI, and overwrites the
remote vessels.parquet + catalog.

Usage:
  python pipeline/v3/rebuild_vessels.py --from 2026-05-26 --to 2026-06-07
  python pipeline/v3/rebuild_vessels.py --from 2026-05-26 --to 2026-06-07 --dry-run
"""

import argparse
import os
import sys
import time
import duckdb
import boto3
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from configuration import *

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SQL_DIR = os.path.join(SCRIPT_DIR, 'sql')
CATALOG_DIR = os.path.join(SCRIPT_DIR, 'catalog')
CATALOG_FILE = os.path.join(CATALOG_DIR, 'ais.ducklake')
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'output')

S3_CATALOG_KEY = "v3/ais.ducklake"
S3_DATA_PREFIX = "v3/ais.ducklake.files"
S3_VESSELS_KEY = f"{S3_DATA_PREFIX}/gold/vessels/vessels.parquet"

BASE_HTTPS = f"https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net"


def s3_client():
    return boto3.client(
        's3',
        endpoint_url=OVH_ENDPOINT,
        aws_access_key_id=OVH_ACCESS_KEY,
        aws_secret_access_key=OVH_SECRET_KEY,
        region_name=OVH_REGION,
        config=boto3.session.Config(s3={'addressing_style': 'path'}),
    )


def list_silver_urls(start: datetime, end: datetime) -> list[str]:
    """Build HTTPS URLs for all silver files in the date range.
    Lists S3 to only include files that actually exist."""
    s3 = s3_client()
    prefix = f"{S3_DATA_PREFIX}/silver/"

    # List all silver files under the prefix
    existing = set()
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=BUCKET_PUBLIC, Prefix=prefix):
        for obj in page.get('Contents', []):
            existing.add(obj['Key'])

    urls = []
    missing = []
    d = start
    while d <= end:
        y, m, day = d.year, f"{d.month:02d}", f"{d.day:02d}"
        key = (
            f"{S3_DATA_PREFIX}/silver/"
            f"year={y}/month={m}/day={day}/messages_consolidated.parquet"
        )
        if key in existing:
            urls.append(f"{BASE_HTTPS}/{key}")
        else:
            missing.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)

    if missing:
        print(f"⚠️  {len(missing)} dates sans silver: {', '.join(missing)}")

    return urls


def rebuild_vessels(silver_urls: list[str], output_path: str, dry_run: bool = False):
    """
    Single DuckDB query across all silver files.
    Extracts static vessel data, deduplicates globally by MMSI.
    """
    print(f"🔍 Lecture de {len(silver_urls)} fichiers silver via HTTPS...")
    for url in silver_urls:
        print(f"   {url}")

    # DuckDB reads Parquet from HTTPS — only the columns we need.
    paths_literal = "[\n    " + ",\n    ".join(f"'{u}'" for u in silver_urls) + "\n]"

    sql = f"""
        COPY (
            SELECT
                mmsi, name, call_sign, imo_number, ship_type,
                length, width, destination, ts AS last_seen_static
            FROM read_parquet({paths_literal})
            WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
              AND name IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY ts DESC) = 1
            ORDER BY mmsi
        ) TO '{output_path}' (FORMAT 'PARQUET', COMPRESSION 'ZSTD')
    """

    if dry_run:
        print(f"\n📝 Dry run — requête SQL:\n{sql[:2000]}...")
        return 0

    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        # Disable HTTP metadata cache to avoid stale Parquet footers
        con.execute("SET enable_http_metadata_cache=false;")
        t0 = time.time()
        con.execute(sql)
        elapsed = time.time() - t0

        count = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{output_path}')"
        ).fetchone()[0]
        print(f"✅ {count:,} navires extraits en {elapsed:.1f}s")
        return count
    finally:
        con.close()


def update_catalog(vessels_url: str):
    """Download catalog, re-register vessels, upload catalog."""
    s3 = s3_client()
    os.makedirs(CATALOG_DIR, exist_ok=True)
    if os.path.exists(CATALOG_FILE):
        os.remove(CATALOG_FILE)

    # Download existing catalog
    try:
        s3.download_file(BUCKET_PUBLIC, S3_CATALOG_KEY, CATALOG_FILE)
        print("✅ Catalogue récupéré via S3")
    except Exception:
        print("❌ Catalogue introuvable sur S3")
        return False

    https_data_path = f"{BASE_HTTPS}/{S3_DATA_PREFIX}/"
    s3_data_path = f"s3://{BUCKET_PUBLIC}/{S3_DATA_PREFIX}/"

    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs; INSTALL ducklake; LOAD httpfs; LOAD ducklake;")
        con.execute(f"SET s3_endpoint='{OVH_ENDPOINT.replace('https://', '')}'")
        con.execute("SET s3_region='gra'")
        con.execute(f"SET s3_access_key_id='{OVH_ACCESS_KEY}'")
        con.execute(f"SET s3_secret_access_key='{OVH_SECRET_KEY}'")
        con.execute("SET s3_url_style='path'; SET s3_use_ssl=true")

        con.execute(f"""
            ATTACH '{CATALOG_FILE}' AS ais_lake (
                TYPE ducklake, DATA_PATH '{s3_data_path}',
                OVERRIDE_DATA_PATH true
            )
        """)

        # Reset vessels registration — delete from metadata directly.
        # DELETE FROM ais_lake.vessels would try to read data files first,
        # which fails if existing files have been overwritten (stale metadata).
        try:
            con.execute(
                "DELETE FROM __ducklake_metadata_ais_lake.ducklake_data_file "
                "WHERE table_id = (SELECT table_id FROM "
                "__ducklake_metadata_ais_lake.ducklake_table "
                "WHERE table_name = 'vessels')"
            )
            print("🗑️  Ancienne registration vessels supprimée")
        except Exception as e:
            print(f"⚠️  DELETE vessels: {e}")

        # Re-register
        con.execute(
            f"CALL ducklake_add_data_files('ais_lake', 'vessels', "
            f"'{vessels_url}', hive_partitioning=false, "
            f"ignore_extra_columns=false)"
        )
        print(f"📋 vessels: {vessels_url}")
    finally:
        con.close()

    # Upload catalog
    s3.upload_file(CATALOG_FILE, BUCKET_PUBLIC, S3_CATALOG_KEY,
                   ExtraArgs={"ACL": "public-read"})
    print(f"✅ Catalogue publié → {BASE_HTTPS}/{S3_CATALOG_KEY}")
    return True


def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rebuild complete vessels table from all silver data"
    )
    parser.add_argument("--from", dest="start_date", type=str, required=True,
                        help="Date début YYYY-MM-DD (inclus)")
    parser.add_argument("--to", dest="end_date", type=str, required=True,
                        help="Date fin YYYY-MM-DD (inclus)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche la requête sans exécuter")
    args = parser.parse_args()

    start = parse_date(args.start_date)
    end = parse_date(args.end_date)
    dates = []
    d = start
    while d <= end:
        dates.append(d)
        d += timedelta(days=1)

    print(f"🚢 Rebuild vessels: {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')} "
          f"({len(dates)} jours)")
    print(f"{'═'*60}")

    # 1. Build silver URL list
    silver_urls = list_silver_urls(start, end)

    # 2. Extract vessels from all silver
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, 'vessels_full.parquet')
    count = rebuild_vessels(silver_urls, output_path, args.dry_run)

    if args.dry_run:
        sys.exit(0)

    if count == 0:
        print("❌ Aucun navire extrait — abandon.")
        sys.exit(1)

    # 3. Upload to S3
    print(f"\n📤 Upload vessels.parquet → S3...")
    s3 = s3_client()
    s3.upload_file(output_path, BUCKET_PUBLIC, S3_VESSELS_KEY,
                   ExtraArgs={"ACL": "public-read"})
    print(f"✅ Uploadé → {BASE_HTTPS}/{S3_VESSELS_KEY}")

    # 4. Update catalog
    print(f"\n📋 Mise à jour du catalogue...")
    vessels_url = f"{BASE_HTTPS}/{S3_VESSELS_KEY}"
    update_catalog(vessels_url)

    # 5. Cleanup
    os.remove(output_path)
    print(f"\n✅ Terminé — {count:,} navires dans vessels.parquet")
