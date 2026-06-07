#!/usr/bin/env python3
"""
DuckLake v3 Pipeline — Fast local transform, DuckLake catalog management.

Architecture (CI-safe, no historical data needed locally):
  1. DOWNLOAD   — ThreadPoolExecutor: NDJSON.zst S3 → /tmp (parallèle)
  2. CONSOLIDATE — DuckDB local: read_ndjson → dedup → COPY TO silver.parquet
  3. DERIVE     — DuckDB local: read silver.parquet → COPY TO 4 gold tables
  4. UPLOAD     — ThreadPoolExecutor: Parquet locaux → S3 public-read
  5. CATALOG    — DuckLake (S3 DATA_PATH):
      a. ducklake_add_data_files pour chaque fichier (silver + gold + vessels)
      b. Upload catalogue → S3

Pourquoi pas DuckLake partout ?
  - Les gros INSERT (consolidation, dérivation) écrivent des centaines de MB.
    Avec DATA_PATH=S3, chaque écriture traverse le réseau → lent.
    Avec COPY TO local → rapide, puis upload parallèle en une fois.
  - Le catalogue gère le partitionnement, le nommage, et la résolution des URLs.
  - Tous les fichiers (y compris vessels) passent par le même pipeline local+upload.

Usage:
  python pipeline/v3/pipeline.py --date YYYY-MM-DD
  python pipeline/v3/pipeline.py --from YYYY-MM-DD --to YYYY-MM-DD
  python pipeline/v3/pipeline.py --date YYYY-MM-DD --force
"""

import argparse
import os
import shutil
import sys
import time
import duckdb
import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from configuration import *

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SQL_DIR = os.path.join(SCRIPT_DIR, 'sql')
WORK_BASE_DIR = os.path.join(SCRIPT_DIR, 'work')          # raw NDJSON.zst downloads (per-date subdirs)
OUTPUT_BASE_DIR = os.path.join(SCRIPT_DIR, 'output')       # generated Parquet files (per-date subdirs)
CATALOG_DIR = os.path.join(SCRIPT_DIR, 'catalog')
CATALOG_FILE = os.path.join(CATALOG_DIR, 'ais.ducklake')

S3_CATALOG_KEY = "v3/ais.ducklake"
S3_DATA_PREFIX = "v3/ais.ducklake.files"


def work_dir(date: datetime) -> str:
    return os.path.join(WORK_BASE_DIR, date.strftime('%Y-%m-%d'))

def output_dir(date: datetime) -> str:
    return os.path.join(OUTPUT_BASE_DIR, date.strftime('%Y-%m-%d'))


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def s3_client():
    return boto3.client(
        's3',
        endpoint_url=OVH_ENDPOINT,
        aws_access_key_id=OVH_ACCESS_KEY,
        aws_secret_access_key=OVH_SECRET_KEY,
        region_name=OVH_REGION,
        config=boto3.session.Config(s3={'addressing_style': 'path'}),
    )


def load_sql(name: str) -> str:
    with open(os.path.join(SQL_DIR, name)) as f:
        return f.read()


def run_sql(con, sql: str, params: dict):
    """Replace :param placeholders and execute."""
    for key, val in params.items():
        if isinstance(val, str):
            quoted = f"'{val}'"
        elif isinstance(val, bool):
            quoted = 'true' if val else 'false'
        elif val is None:
            quoted = 'NULL'
        else:
            quoted = str(val)
        sql = sql.replace(f':{key}', quoted)
    con.execute(sql)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════

def list_raw_files(s3, target_date: datetime) -> list[str]:
    prefix = (
        f"raw/year={target_date.year}"
        f"/month={target_date.month:02d}"
        f"/day={target_date.day:02d}/"
    )
    paginator = s3.get_paginator('list_objects_v2')
    return [
        obj['Key']
        for page in paginator.paginate(Bucket=BUCKET_RAW, Prefix=prefix)
        for obj in page.get('Contents', [])
    ]


def download_files(s3, keys: list[str], dest_dir: str,
                   max_workers: int = 16) -> tuple[int, int]:
    os.makedirs(dest_dir, exist_ok=True)
    downloaded, failed = 0, 0

    def _dl(key):
        local = os.path.join(dest_dir, os.path.basename(key))
        try:
            s3.download_file(BUCKET_RAW, key, local)
            return (None, local)
        except Exception as e:
            return (str(e), None)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_dl, k): k for k in keys}
        for future in as_completed(futures):
            err, _ = future.result()
            if err:
                failed += 1
                if failed <= 3:
                    print(f"   ⚠️ Download failed: {futures[future]} — {err}")
            else:
                downloaded += 1
    return downloaded, failed


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2-3: LOCAL TRANSFORM (no DuckLake, pure DuckDB)
# ═══════════════════════════════════════════════════════════════════════════════

def local_transform(target_date: datetime, work_dir_path: str,
                     output_dir_path: str, tables: set | None = None) -> dict:
    """
    Consolidate + Derive locally. No DuckLake, no S3 — pure local DuckDB.
    If tables is specified, only those tables are generated.
    'messages' = silver consolidation; all others = gold derivation.
    Returns stats + paths of generated files.
    """
    all_tables = tables is None
    want_messages = all_tables or 'messages' in tables

    os.makedirs(output_dir_path, exist_ok=True)

    silver_dir = os.path.join(
        output_dir_path,
        f"silver/year={target_date.year}"
        f"/month={target_date.month:02d}"
        f"/day={target_date.day:02d}",
    )
    gold_dir = os.path.join(output_dir_path, "gold")
    os.makedirs(silver_dir, exist_ok=True)
    os.makedirs(gold_dir, exist_ok=True)

    silver_file = os.path.join(silver_dir, "messages_consolidated.parquet")
    raw_glob = os.path.join(work_dir_path, "*.ndjson.zst")

    con = duckdb.connect()
    stats = {}
    t0 = time.time()

    try:
        # ── Consolidate (messages / silver) ──────────────────────────────────
        if want_messages:
            n_raw = len([f for f in os.listdir(work_dir_path) if f.endswith('.ndjson.zst')])
            print(f"   📦 Consolidation: {n_raw} fichiers NDJSON → Parquet...")
            run_sql(con, load_sql('01_consolidate.sql'), {
                'raw_path': raw_glob,
                'output_path': silver_file,
            })
            stats['messages'] = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{silver_file}')"
            ).fetchone()[0]
            print(f"   ✅ messages: {stats['messages']:,} lignes ({time.time()-t0:.1f}s)")
        else:
            # Download existing silver from S3 if not present locally
            if not os.path.exists(silver_file):
                print("   📥 Téléchargement silver existant depuis S3...")
                s3 = s3_client()
                base_https = f"https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net"
                y, m, d = target_date.year, f"{target_date.month:02d}", f"{target_date.day:02d}"
                silver_key = (
                    f"{S3_DATA_PREFIX}/silver/year={y}/month={m}/day={d}"
                    f"/messages_consolidated.parquet"
                )
                try:
                    s3.download_file(BUCKET_PUBLIC, silver_key, silver_file)
                    print(f"   ✅ Silver téléchargé")
                except Exception as e:
                    print(f"   ❌ Silver introuvable dans S3: {e}")
                    return stats
            else:
                print(f"   ♻️  Silver existant: {silver_file}")

        # ── Derive ─────────────────────────────────────────────────────────
        want_gold = all_tables or bool(tables & {'vessels_positions', 'vessel_tracks', 'base_stations', 'aids_to_navigation'})
        if not want_gold and 'vessels' not in (tables or set()):
            return stats

        if want_gold:
            print("   🏗️  Dérivation gold...")
            t1 = time.time()

        # Hive-style partition paths for DuckLake compatibility
        y, m, d = target_date.year, f"{target_date.month:02d}", f"{target_date.day:02d}"
        date_str = target_date.strftime('%Y-%m-%d')

        vp_dir = os.path.join(gold_dir, 'vessels_positions',
                              f'year={y}', f'month={m}', f'day={d}')
        vt_dir = os.path.join(gold_dir, 'vessel_tracks', f'date={date_str}')
        bs_dir = os.path.join(gold_dir, 'base_stations',
                              f'year={y}', f'month={m}', f'day={d}')
        an_dir = os.path.join(gold_dir, 'aids_to_navigation',
                              f'year={y}', f'month={m}', f'day={d}')

        derive_tables = tables if not all_tables else {'vessels_positions', 'vessel_tracks', 'base_stations', 'aids_to_navigation'}

        # Always create dirs for tables we might write
        for t, dpath in [('vessels_positions', vp_dir), ('vessel_tracks', vt_dir),
                         ('base_stations', bs_dir), ('aids_to_navigation', an_dir)]:
            if t in derive_tables:
                os.makedirs(dpath, exist_ok=True)

        # Build derive SQL — only selected tables
        derive_sql = load_sql('02_derive.sql')
        # Split by COPY statements, keep only requested tables
        import re
        blocks = re.split(r'\n(?=-- \d+\. )', derive_sql)
        selected_blocks = [blocks[0]]  # header comments
        table_patterns = {
            'vessels_positions': 'vessels_positions',
            'vessel_tracks': 'vessel_tracks',
            'base_stations': 'base_stations',
            'aids_to_navigation': 'aids_to_navigation',
        }
        for block in blocks[1:]:
            for tbl, pattern in table_patterns.items():
                if tbl in derive_tables and pattern in block.split('\n')[0].lower():
                    selected_blocks.append(block)
                    break
        filtered_sql = '\n'.join(selected_blocks)

        if filtered_sql.strip():
            run_sql(con, filtered_sql, {
                'silver_path': silver_file,
                'vessels_positions_path':
                    os.path.join(vp_dir, 'vessels_positions.parquet'),
                'vessel_tracks_path':
                    os.path.join(vt_dir, 'vessel_tracks.parquet'),
                'base_stations_path':
                    os.path.join(bs_dir, 'base_stations.parquet'),
                'aids_to_navigation_path':
                    os.path.join(an_dir, 'aids_to_navigation.parquet'),
            })

        for table in derive_tables:
            if table == 'vessel_tracks':
                p = os.path.join(gold_dir, table, f'date={date_str}', f'{table}.parquet')
            else:
                p = os.path.join(gold_dir, table,
                                 f'year={y}', f'month={m}', f'day={d}',
                                 f'{table}.parquet')
            if os.path.exists(p):
                stats[table] = con.execute(
                    f"SELECT COUNT(*) FROM read_parquet('{p}')"
                ).fetchone()[0]
            else:
                stats[table] = 0

        if want_gold:
            print(f"   ✅ Gold ({time.time()-t1:.1f}s): "
                  + ', '.join(f'{k}={v:,}' for k, v in stats.items() if k != 'messages'))

        # ── Vessels ───────────────────────────────────────────────────────
        want_vessels = all_tables or 'vessels' in tables
        if want_vessels:
            print("   🚢 Vessels...")
            t2 = time.time()
            vessels_dir = os.path.join(gold_dir, 'vessels')
            os.makedirs(vessels_dir, exist_ok=True)
            vessels_file = os.path.join(vessels_dir, 'vessels.parquet')

            # Download existing vessels for merge (fail gracefully if first run)
            existing_vessels_path = os.path.join(
                gold_dir, 'vessels_existing.parquet'
            )
            existing_key = f"{S3_DATA_PREFIX}/gold/vessels/vessels.parquet"
            has_existing = False
            try:
                s3_client().download_file(
                    BUCKET_PUBLIC, existing_key, existing_vessels_path
                )
                has_existing = True
                print("   📥 Vessels existant téléchargé pour merge")
            except Exception:
                print("   🆕 Premier run vessels — pas de merge")

            if has_existing:
                run_sql(con, load_sql('03_vessels.sql'), {
                    'silver_path': silver_file,
                    'existing_vessels_path': existing_vessels_path,
                    'output_path': vessels_file,
                })
                # Clean up temp file
                try:
                    os.remove(existing_vessels_path)
                except OSError:
                    pass
            else:
                # First run: extract from today's silver only
                run_sql(con, load_sql('03_vessels_from_scratch.sql'), {
                    'silver_path': silver_file,
                    'output_path': vessels_file,
                })

            stats['vessels'] = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{vessels_file}')"
            ).fetchone()[0]
            print(f"   ✅ vessels: {stats['vessels']:,} navires ({time.time()-t2:.1f}s)")
    finally:
        con.close()

    return stats


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4: UPLOAD PARQUET FILES
# ═══════════════════════════════════════════════════════════════════════════════

def upload_output_files(s3, output_dir_path: str,
                       max_workers: int = 32) -> list[tuple[str, str]]:
    """
    Upload all generated Parquet files to S3.
    Returns list of (local_path, s3_url) for catalog registration.
    """
    all_uploads = []
    for root, _, filenames in os.walk(output_dir_path):
        for f in filenames:
            if not f.endswith('.parquet'):
                continue
            local_path = os.path.join(root, f)
            rel = os.path.relpath(local_path, output_dir_path)
            s3_key = f"{S3_DATA_PREFIX}/{rel}"
            all_uploads.append((local_path, s3_key))

    if not all_uploads:
        return []

    print(f"   📤 Upload de {len(all_uploads)} fichiers Parquet → S3...")

    def _upload(args):
        local_path, s3_key = args
        try:
            s3.upload_file(local_path, BUCKET_PUBLIC, s3_key,
                          ExtraArgs={"ACL": "public-read"})
            return (s3_key, None)
        except Exception as e:
            return (s3_key, str(e))

    uploaded = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_upload, u): u for u in all_uploads}
        for i, future in enumerate(as_completed(futures)):
            _, err = future.result()
            if not err:
                uploaded += 1
            if (i + 1) % 50 == 0:
                print(f"   📤 {i+1}/{len(all_uploads)}...")

    print(f"   ✅ {uploaded}/{len(all_uploads)} fichiers uploadés")

    # Build URL list for catalog registration
    base_https = f"https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net"
    return [
        (local_path, f"{base_https}/{s3_key}")
        for local_path, s3_key in all_uploads
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5: CATALOG (DuckLake on S3)
# ═══════════════════════════════════════════════════════════════════════════════

def update_catalog(s3, uploaded_files: list[tuple[str, str]],
                   target_date: datetime, force: bool,
                   output_dir_path: str = '',
                   tables: set | None = None):
    """
    Download catalog, register new files, upsert vessels, upload catalog.
    Uses DuckLake with S3 DATA_PATH — small operations, latency acceptable.
    """
    os.makedirs(CATALOG_DIR, exist_ok=True)
    if os.path.exists(CATALOG_FILE):
        os.remove(CATALOG_FILE)

    base_https = f"https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net"
    # Use S3 DATA_PATH for writing (DuckLake needs S3 API, not HTTPS, to write files).
    s3_data_path = f"s3://{BUCKET_PUBLIC}/{S3_DATA_PREFIX}/"

    # Download existing catalog
    is_new = False
    try:
        s3.download_file(BUCKET_PUBLIC, S3_CATALOG_KEY, CATALOG_FILE)
        print("   ✅ Catalogue récupéré via S3")
    except Exception:
        try:
            import requests
            r = requests.get(f"{base_https}/{S3_CATALOG_KEY}", timeout=10)
            if r.status_code == 200:
                with open(CATALOG_FILE, 'wb') as f:
                    f.write(r.content)
                print("   ✅ Catalogue récupéré via HTTPS")
            else:
                raise Exception(f"HTTP {r.status_code}")
        except Exception:
            is_new = True
            print("   🆕 Nouveau catalogue")

    # ── DuckLake setup ────────────────────────────────────────────────────
    con = duckdb.connect()
    con.execute("INSTALL httpfs; INSTALL ducklake; LOAD httpfs; LOAD ducklake;")
    con.execute(f"SET s3_endpoint='{OVH_ENDPOINT.replace('https://', '')}'")
    con.execute("SET s3_region='gra'")
    con.execute(f"SET s3_access_key_id='{OVH_ACCESS_KEY}'")
    con.execute(f"SET s3_secret_access_key='{OVH_SECRET_KEY}'")
    con.execute("SET s3_url_style='path'; SET s3_use_ssl=true")

    # Store public HTTPS DATA_PATH in catalog (for read-only clients).
    # Use S3 DATA_PATH for our session (needed for writes).
    https_data_path = f"{base_https}/{S3_DATA_PREFIX}/"

    if is_new:
        con.execute(f"""
            ATTACH '{CATALOG_FILE}' AS ais_lake (
                TYPE ducklake, DATA_PATH '{https_data_path}',
                OVERRIDE_DATA_PATH true, AUTOMATIC_MIGRATION true
            )
        """)
        con.execute("DETACH ais_lake")
        print(f"💾 DATA_PATH public: {https_data_path}")

    # Attach with S3 DATA_PATH for read+write operations.
    con.execute(f"""
        ATTACH '{CATALOG_FILE}' AS ais_lake (
            TYPE ducklake, DATA_PATH '{s3_data_path}',
            OVERRIDE_DATA_PATH true
        )
    """)

    try:
        # ── Create tables if new ─────────────────────────────────────────
        if is_new:
            _create_all_tables(con)
        else:
            if force:
                # Clean old partitions before schema migration to avoid type mismatch
                _clean_partitions(con, target_date, tables)
            _migrate_schema(con, tables)

        # ── Register files ───────────────────────────────────────────────
        known = {
            row[0] for row in con.execute(
                "SELECT path FROM __ducklake_metadata_ais_lake.ducklake_data_file"
            ).fetchall()
        }

        # Map local path → (table_name, hive_partitioning)
        file_registry = _classify_files(uploaded_files, output_dir_path)

        for local_path, url in uploaded_files:
            info = file_registry.get(local_path)
            if not info:
                continue
            table_name, hive = info
            # Skip tables not requested
            if tables is not None and table_name not in tables:
                continue
            # Vessels is a single overwritten file — always re-register.
            # Other tables skip if URL already known (unless --force).
            if table_name != 'vessels' and url in known and not force:
                continue
            # messages table in existing v2 catalog has GENERATED ALWAYS columns
            # for year/month/day — our Parquet has them as regular columns.
            ignore = 'true' if table_name == 'messages' else 'false'
            # Reset vessels registration before re-adding (single overwritten file).
            # Delete from metadata table directly — DELETE FROM ais_lake.vessels
            # would try to read the data files first, which fails if they've
            # already been overwritten (stale footer metadata).
            if table_name == 'vessels' and url in known:
                try:
                    con.execute(
                        "DELETE FROM __ducklake_metadata_ais_lake.ducklake_data_file "
                        "WHERE table_id = (SELECT table_id FROM "
                        "__ducklake_metadata_ais_lake.ducklake_table "
                        "WHERE table_name = 'vessels')"
                    )
                except Exception:
                    pass
            con.execute(
                f"CALL ducklake_add_data_files('ais_lake', '{table_name}', "
                f"'{url}', hive_partitioning={str(hive).lower()}, "
                f"ignore_extra_columns={ignore})"
            )
            print(f"   📋 {table_name}: {url}")

    finally:
        con.close()

    # ── Upload catalog ───────────────────────────────────────────────────
    s3.upload_file(CATALOG_FILE, BUCKET_PUBLIC, S3_CATALOG_KEY,
                   ExtraArgs={"ACL": "public-read"})
    print(f"   ✅ Catalogue publié → {base_https}/{S3_CATALOG_KEY}")


def _classify_files(uploaded_files: list[tuple[str, str]],
                    output_dir_path: str) -> dict:
    """Map local_path → (table_name, hive_partitioning).
    Silver (messages): Hive path silver/year=.../month=.../day=.../ → hive=true.
    Gold tables: also Hive paths → hive=true.
    vessels: flat path, non-partitioned → hive=false.
    """
    mapping = {}
    for local_path, _ in uploaded_files:
        rel = os.path.relpath(local_path, output_dir_path)
        if rel.startswith('silver/'):
            mapping[local_path] = ('messages', True)
        elif rel.startswith('gold/vessels_positions/'):
            mapping[local_path] = ('vessels_positions', True)
        elif rel.startswith('gold/vessel_tracks/'):
            mapping[local_path] = ('vessel_tracks', True)
        elif rel.startswith('gold/base_stations/'):
            mapping[local_path] = ('base_stations', True)
        elif rel.startswith('gold/aids_to_navigation/'):
            mapping[local_path] = ('aids_to_navigation', True)
        elif rel.startswith('gold/vessels/'):
            mapping[local_path] = ('vessels', False)
    return mapping


def _create_all_tables(con):
    """Create all DuckLake tables (first run)."""
    tables = [
        ("messages", """
            CREATE TABLE IF NOT EXISTS ais_lake.messages (
                message_type VARCHAR, mmsi BIGINT, ts TIMESTAMPTZ,
                lat DOUBLE, lon DOUBLE, received_at TIMESTAMPTZ,
                source_listener VARCHAR,
                sog DOUBLE, cog DOUBLE, true_heading INTEGER,
                navigational_status INTEGER, rate_of_turn INTEGER,
                message_id INTEGER, position_accuracy BOOLEAN,
                raim BOOLEAN, valid BOOLEAN,
                name VARCHAR, call_sign VARCHAR,
                imo_number BIGINT, ship_type INTEGER, ais_version INTEGER,
                length DOUBLE, width DOUBLE,
                dimension_a DOUBLE, dimension_b DOUBLE,
                dimension_c DOUBLE, dimension_d DOUBLE,
                max_static_draught DOUBLE,
                destination VARCHAR, eta TIMESTAMPTZ, dte BOOLEAN,
                fix_type INTEGER, type_of_aton INTEGER,
                off_position BOOLEAN, virtual_aton BOOLEAN,
                raw_message VARCHAR, metadata_json VARCHAR,
                year INTEGER, month INTEGER, day INTEGER
            )
        """),
        ("vessels_positions", """
            CREATE TABLE IF NOT EXISTS ais_lake.vessels_positions (
                message_type VARCHAR, mmsi BIGINT, ts TIMESTAMPTZ,
                lat DOUBLE, lon DOUBLE, received_at TIMESTAMPTZ,
                source_listener VARCHAR,
                sog DOUBLE, cog DOUBLE, true_heading INTEGER,
                navigational_status INTEGER, rate_of_turn INTEGER,
                message_id INTEGER, position_accuracy BOOLEAN,
                raim BOOLEAN, valid BOOLEAN,
                year INTEGER, month INTEGER, day INTEGER
            )
        """),
        ("vessel_tracks", """
            CREATE TABLE IF NOT EXISTS ais_lake.vessel_tracks (
                mmsi INTEGER, ts INTEGER, lat INTEGER, lon INTEGER,
                heading INTEGER, date DATE
            )
        """),
        ("base_stations", """
            CREATE TABLE IF NOT EXISTS ais_lake.base_stations (
                mmsi BIGINT, ts TIMESTAMPTZ, lat DOUBLE, lon DOUBLE,
                received_at TIMESTAMPTZ, source_listener VARCHAR,
                message_id INTEGER, raim BOOLEAN,
                year INTEGER, month INTEGER, day INTEGER
            )
        """),
        ("aids_to_navigation", """
            CREATE TABLE IF NOT EXISTS ais_lake.aids_to_navigation (
                mmsi BIGINT, name VARCHAR, type_of_aton INTEGER,
                ts TIMESTAMPTZ, lat DOUBLE, lon DOUBLE,
                dimension_a DOUBLE, dimension_b DOUBLE,
                dimension_c DOUBLE, dimension_d DOUBLE,
                off_position BOOLEAN, virtual_aton BOOLEAN, raim BOOLEAN,
                received_at TIMESTAMPTZ, source_listener VARCHAR,
                year INTEGER, month INTEGER, day INTEGER
            )
        """),
        ("vessels", """
            CREATE TABLE IF NOT EXISTS ais_lake.vessels (
                mmsi BIGINT, name VARCHAR, call_sign VARCHAR,
                imo_number BIGINT, ship_type INTEGER,
                length DOUBLE, width DOUBLE, destination VARCHAR,
                last_seen_static TIMESTAMPTZ
            )
        """),
    ]
    partitions = {
        "messages":              "year, month, day",
        "vessels_positions":     "year, month, day",
        "vessel_tracks":         "date",
        "base_stations":         "year, month, day",
        "aids_to_navigation":    "year, month, day",
    }

    for table_name, create_sql in tables:
        try:
            con.execute(create_sql)
        except Exception as e:
            print(f"   ⚠️ CREATE {table_name}: {e}")

    for table_name, cols in partitions.items():
        try:
            con.execute(
                f"ALTER TABLE ais_lake.{table_name} "
                f"SET PARTITIONED BY ({cols})"
            )
        except Exception:
            pass

    print("   🗂️  Tables créées")


def _migrate_schema(con, tables: set | None = None):
    """Handle schema evolution for existing tables."""
    # Ensure heading column exists on vessel_tracks (added 2026-06-07)
    if tables is None or 'vessel_tracks' in tables:
        try:
            con.execute(
                "ALTER TABLE ais_lake.vessel_tracks "
                "ADD COLUMN IF NOT EXISTS heading INTEGER"
            )
        except Exception:
            pass  # DuckLake may not support ALTER TABLE ADD COLUMN


def _clean_partitions(con, target_date: datetime, tables: set | None = None):
    """Delete existing partitions for target_date (--force mode).
    Deletes from ducklake_data_file metadata table directly — avoids
    DuckLake trying to read data files that may have already been
    overwritten (stale footer metadata → "No magic bytes" error).
    """
    all_tables = tables is None
    y, m, d = target_date.year, f"{target_date.month:02d}", target_date.day
    date_str = target_date.strftime('%Y-%m-%d')

    # Path patterns for partition matching (LIKE on S3 path)
    patterns = {
        'messages':              f"%silver/year={y}/month={m}/day={d}/%",
        'vessels_positions':     f"%vessels_positions/year={y}/month={m}/day={d}/%",
        'vessel_tracks':         f"%vessel_tracks/date={date_str}/%",
        'base_stations':         f"%base_stations/year={y}/month={m}/day={d}/%",
        'aids_to_navigation':    f"%aids_to_navigation/year={y}/month={m}/day={d}/%",
        'vessels':               f"%vessels/vessels.parquet%",
    }
    for table_name, path_pattern in patterns.items():
        if not all_tables and table_name not in (tables or set()):
            continue
        try:
            result = con.execute(f"""
                DELETE FROM __ducklake_metadata_ais_lake.ducklake_data_file
                WHERE table_id = (
                    SELECT table_id FROM __ducklake_metadata_ais_lake.ducklake_table
                    WHERE table_name = '{table_name}'
                )
                AND path LIKE '{path_pattern}'
            """)
            deleted = result.fetchone()[0] if result else 0
            if deleted > 0:
                print(f"   🗑️  {table_name}: {deleted} fichier(s) "
                      f"(partition {date_str})")
        except Exception as e:
            print(f"   ⚠️ DELETE {table_name}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def process_date(target_date: datetime, force: bool = False, no_download: bool = False,
                  tables: set | None = None):
    ts = target_date.strftime('%Y-%m-%d')
    print(f"\n{'═'*70}")
    print(f"📅 {ts}")
    if tables:
        print(f"   🎯 Tables: {', '.join(sorted(tables))}")
    print(f"{'═'*70}")

    s3 = s3_client()

    wd = work_dir(target_date)
    od = output_dir(target_date)

    # 1. List raw files
    keys = list_raw_files(s3, target_date)
    print(f"🔍 {len(keys)} fichiers raw trouvés")
    if not keys:
        print("⚠️ Aucune donnée pour cette date.")
        return

    # Cache check: per-date work dir, exact file count match
    os.makedirs(wd, exist_ok=True)
    existing = [f for f in os.listdir(wd) if f.endswith('.ndjson.zst')]
    expected = len(keys)
    if no_download or len(existing) == expected:
        if no_download:
            print(f"📂 --no-download: {len(existing)} fichiers dans {wd}, skip")
        else:
            print(f"📂 Cache: {len(existing)}/{expected} fichiers déjà là, skip download")
        downloaded, failed = len(existing), 0
    else:
        # Clean stale files (wrong date or partial download)
        for f in existing:
            os.remove(os.path.join(wd, f))

        t0 = time.time()
        print(f"📥 Download {expected} fichiers NDJSON.zst → {wd}...")
        downloaded, failed = download_files(s3, keys, wd)
        print(f"   ✅ {downloaded} ok, {failed} fail ({time.time()-t0:.1f}s)")

    if downloaded == 0:
        print("❌ Aucun fichier téléchargé.")
        return

    # 2-3. Local transform (consolidate + derive)
    t_start = time.time()
    stats = local_transform(target_date, wd, od, tables)
    print(f"   ⏱️  Transform locale: {time.time()-t_start:.1f}s")

    # 4. Upload Parquet files
    t2 = time.time()
    uploaded_files = upload_output_files(s3, od)
    print(f"   ⏱️  Upload: {time.time()-t2:.1f}s")

    # 5. Update catalog + upsert vessels
    t3 = time.time()
    update_catalog(s3, uploaded_files, target_date, force, od, tables)
    print(f"   ⏱️  Catalogue: {time.time()-t3:.1f}s")

    # Cleanup output (keep work dir for caching)
    shutil.rmtree(od, ignore_errors=True)

    print(f"\n✅ {ts} terminé en {time.time()-t_start:.1f}s")
    for t in ['messages', 'vessels_positions', 'vessel_tracks', 'base_stations', 'aids_to_navigation', 'vessels']:
        if t in stats:
            print(f"   {t:<20} {stats[t]:,}")


def date_range(start: datetime, end: datetime) -> list[datetime]:
    days = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    return days


def parse_date(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"❌ Format invalide: '{s}'. Utilisez YYYY-MM-DD.")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DuckLake v3 Pipeline — Fast local transform + DuckLake catalog"
    )
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
                             "(messages, vessels_positions, vessel_tracks, "
                             "base_stations, aids_to_navigation, vessels). "
                             "Défaut: toutes. Si 'messages' absent, "
                             "télécharge le silver existant depuis S3.")
    args = parser.parse_args()

    tables = set(t.strip() for t in args.tables.split(',')) if args.tables else None
    if tables:
        valid = {'messages', 'vessels_positions', 'vessel_tracks',
                 'base_stations', 'aids_to_navigation', 'vessels'}
        unknown = tables - valid
        if unknown:
            print(f"❌ Tables inconnues: {', '.join(sorted(unknown))}")
            print(f"   Valides: {', '.join(sorted(valid))}")
            sys.exit(1)

    if args.start_date and args.end_date:
        dates = date_range(parse_date(args.start_date), parse_date(args.end_date))
    elif args.start_date:
        print("❌ --from nécessite --to"); sys.exit(1)
    elif args.end_date:
        print("❌ --to nécessite --from"); sys.exit(1)
    elif args.date:
        dates = [parse_date(args.date)]
    else:
        dates = [datetime.now(timezone.utc) - timedelta(days=1)]

    for d in dates:
        process_date(d, force=args.force, no_download=args.no_download, tables=tables)
