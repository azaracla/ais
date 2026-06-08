#!/usr/bin/env python3
"""
NOAA AIS CSV → Parquet hyper optimisé

Télécharge les fichiers .csv.zst depuis le blob NOAA et les transforme en Parquet
avec normalisation des infos navires et compression ZSTD maximale.

Schema NOAA (17 colonnes) :
  mmsi, base_date_time, longitude, latitude, sog, cog, heading,
  vessel_name, imo, call_sign, vessel_type, status, length, width,
  draft, cargo, transceiver_class

Tables produites (dans --output) :
  positions/    date=YYYY-MM-DD/              (hive partitionné par jour)
  vessels/      vessels.parquet               (flat, dédupliqué par mmsi)

Optimisations :
  - lat/lon FLOAT (4B au lieu de DOUBLE 8B, precision ~1m)
  - ts en INT32 (epoch seconds)
  - heading SMALLINT, status TINYINT, vessel_type/cargo SMALLINT
  - transceiver_class A→1, B→2 (TINYINT)
  - imo: "IMO9212424" → INTEGER 9212424
  - Normalisation vessels : nom, imo, call_sign, dimensions → table séparée
  - ZSTD compression level 16, row groups 100k, trié mmsi+ts, partitionné date=YYYY-MM-DD

Usage:
  python scripts/noaa_to_parquet.py --date 2025-01-01
  python scripts/noaa_to_parquet.py --from 2025-01-01 --to 2025-01-05
  python scripts/noaa_to_parquet.py --year 2025
"""

import argparse
import os
import queue
import sys
import threading
import time
import duckdb
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
NOAA_BASE = "https://noaaocm.blob.core.windows.net/ais/csv2"
DOWNLOAD_DIR = os.path.join(PROJECT_ROOT, "noaa_downloads")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "noaa_output")

# ZSTD compression level (DuckDB supports 1-22)
ZSTD_LEVEL = 16
MIN_ZST_SIZE = 1_000_000  # 1 MB minimum, un fichier NOAA valide fait ~150-290 MB


def process_date(con, zst_path: str, date_str: str, output_base: str):
    """Lit un .csv.zst NOAA → positions.parquet + extrait les vessels.
    Retourne (n_positions, n_vessels)."""
    positions_dir = os.path.join(output_base, "positions", f"date={date_str}")
    os.makedirs(positions_dir, exist_ok=True)
    positions_file = os.path.join(positions_dir, "positions.parquet")
    t0 = time.time()

    y_int, m_int, d_int = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE noaa_raw AS
        SELECT
            try_cast(mmsi AS INTEGER)                                       AS mmsi,
            try_cast(epoch(try_cast(base_date_time AS TIMESTAMP)) AS INTEGER) AS ts,
            try_cast(latitude AS FLOAT)                                     AS lat,
            try_cast(longitude AS FLOAT)                                    AS lon,
            try_cast(sog AS FLOAT)                                          AS sog,
            try_cast(cog AS FLOAT)                                          AS cog,
            try_cast(nullif(heading, '') AS SMALLINT)                       AS heading,
            nullif(vessel_name, '')::VARCHAR                                AS vessel_name,
            nullif(nullif(imo, ''), '')::VARCHAR                            AS imo_raw,
            nullif(call_sign, '')::VARCHAR                                  AS call_sign,
            try_cast(vessel_type AS SMALLINT)                               AS vessel_type,
            try_cast(status AS TINYINT)                                     AS status,
            try_cast(nullif(length, '') AS SMALLINT)                        AS length_m,
            try_cast(nullif(width, '') AS SMALLINT)                         AS width_m,
            try_cast(nullif(draft, '') AS FLOAT)                            AS draft,
            try_cast(nullif(cargo, '') AS SMALLINT)                         AS cargo,
            CASE
                WHEN transceiver IS NULL OR transceiver = '' THEN NULL
                WHEN upper(transceiver) = 'A' THEN 1::TINYINT
                WHEN upper(transceiver) = 'B' THEN 2::TINYINT
            END                                                             AS transceiver_class
        FROM read_csv('{zst_path}',
            header=true,
            all_varchar=true,
            ignore_errors=true,
            sample_size=200000,
            delim=',',
            null_padding=true,
            nullstr=['', 'NA', 'N/A', 'nan', 'none', 'None', 'NULL']
        )
    """)

    # ── Positions ─────────────────────────────────────────────────────
    con.execute(f"""
        COPY (
            SELECT
                mmsi, ts, lat, lon, sog, cog, heading, status, cargo, draft,
                transceiver_class, vessel_type,
                CAST('{date_str}' AS DATE) AS date
            FROM noaa_raw
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY mmsi ASC, ts ASC
        ) TO '{positions_file}' (
            FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL {ZSTD_LEVEL},
            ROW_GROUP_SIZE 100000
        )
    """)

    n_pos = con.execute(f"SELECT COUNT(*) FROM read_parquet('{positions_file}')").fetchone()[0]

    # ── Extraire les vessels de ce jour (garder dans une table temp) ───
    con.execute("""
        CREATE OR REPLACE TEMP TABLE today_vessels AS
        SELECT DISTINCT ON (mmsi)
            mmsi,
            vessel_name AS name,
            CASE
                WHEN imo_raw LIKE 'IMO%%'
                    THEN NULLIF(regexp_replace(imo_raw, '^IMO', ''), '')::INTEGER
                ELSE NULLIF(imo_raw, '')::INTEGER
            END AS imo_number,
            call_sign,
            vessel_type,
            length_m AS length,
            width_m AS width,
            transceiver_class
        FROM noaa_raw
        WHERE mmsi IS NOT NULL
    """)
    n_vessels_extracted = con.execute("SELECT COUNT(*) FROM today_vessels").fetchone()[0]

    elapsed = time.time() - t0
    pos_size = os.path.getsize(positions_file) / 1e6
    print(f"   ✅ {date_str}: positions={n_pos:,} ({pos_size:.1f}MB) "
          f"vessels={n_vessels_extracted:,} [{elapsed:.1f}s]")

    return n_pos, n_vessels_extracted


def merge_vessels(con, output_base: str):
    """Merge les vessels du jour (today_vessels) avec le vessels.parquet existant.
    Si premier jour, crée directement. Garde l'enregistrement le plus complet par MMSI."""
    vessels_dir = os.path.join(output_base, "vessels")
    os.makedirs(vessels_dir, exist_ok=True)
    vessels_file = os.path.join(vessels_dir, "vessels.parquet")
    target_cols = "mmsi, name, imo_number, call_sign, vessel_type, length, width, transceiver_class"

    if os.path.exists(vessels_file):
        con.execute(f"CREATE OR REPLACE TEMP TABLE existing_vessels AS SELECT * FROM read_parquet('{vessels_file}')")
        con.execute(f"""
            COPY (
                SELECT DISTINCT ON (mmsi) {target_cols}
                FROM (
                    SELECT *, (
                        (CASE WHEN name    IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN call_sign IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN imo_number IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN length  IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN width   IS NOT NULL THEN 1 ELSE 0 END)
                    )::TINYINT AS _score
                    FROM today_vessels
                    WHERE name IS NOT NULL AND name != ''
                    UNION ALL
                    SELECT *, (
                        (CASE WHEN name    IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN call_sign IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN imo_number IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN length  IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN width   IS NOT NULL THEN 1 ELSE 0 END)
                    )::TINYINT AS _score
                    FROM existing_vessels
                ) ORDER BY mmsi, _score DESC
            ) TO '{vessels_file}' (
                FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL {ZSTD_LEVEL},
                ROW_GROUP_SIZE 100000
            )
        """)
    else:
        con.execute(f"""
            COPY (
                SELECT {target_cols}
                FROM today_vessels
                WHERE name IS NOT NULL AND name != ''
                ORDER BY mmsi
            ) TO '{vessels_file}' (
                FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL {ZSTD_LEVEL},
                ROW_GROUP_SIZE 100000
            )
        """)

    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{vessels_file}')").fetchone()[0]
    return n


def show_schema(con, output_base: str):
    """Affiche les types Parquet pour vérification."""
    for table, glob_pat in [
        ("positions",
         os.path.join(output_base, "positions", "*", "*.parquet")),
        ("vessels",
         os.path.join(output_base, "vessels", "*.parquet")),
    ]:
        try:
            rows = con.execute(
                f"SELECT column_name, column_type FROM parquet_schema('{glob_pat}')"
            ).fetchall()
            print(f"\n  📋 {table}:")
            for col, t in rows:
                print(f"     {col:<25} {t}")
        except Exception:
            pass


def show_file_sizes(output_base: str):
    """Affiche les tailles des fichiers Parquet."""
    total = 0
    print(f"\n  📁 Fichiers Parquet ({output_base}/):")
    for root, _, files in sorted(os.walk(output_base)):
        for f in sorted(files):
            if f.endswith(".parquet"):
                path = os.path.join(root, f)
                size = os.path.getsize(path)
                total += size
                rel = os.path.relpath(path, output_base)
                print(f"     {rel:<55} {size/1e6:>8.1f} MB")
    print(f"     {'─'*63}")
    print(f"     {'TOTAL':<55} {total/1e6:>8.1f} MB")


def main():
    parser = argparse.ArgumentParser(
        description="NOAA AIS CSV → Parquet hyper optimisé (ZSTD lv16+)"
    )
    parser.add_argument("--date", type=str, help="Date YYYY-MM-DD")
    parser.add_argument("--from", dest="start", type=str, help="Date début")
    parser.add_argument("--to", dest="end", type=str, help="Date fin")
    parser.add_argument("--year", type=int, help="Année entière")
    parser.add_argument("--dl-workers", type=int, default=4,
                        help="Téléchargements parallèles (défaut: 4)")
    parser.add_argument("--no-download", action="store_true",
                        help="Utiliser les fichiers locaux (skip download)")
    parser.add_argument("--output", type=str, default=OUTPUT_DIR)
    parser.add_argument("--download-dir", type=str, default=DOWNLOAD_DIR)
    args = parser.parse_args()

    # ── Dates ────────────────────────────────────────────────────────────
    if args.year:
        import calendar
        dates = []
        for m in range(1, 13):
            for d in range(1, calendar.monthrange(args.year, m)[1] + 1):
                dates.append(f"{args.year}-{m:02d}-{d:02d}")
    elif args.start and args.end:
        from datetime import datetime, timedelta
        s, e = datetime.strptime(args.start, "%Y-%m-%d"), datetime.strptime(args.end, "%Y-%m-%d")
        dates = [(s + timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range((e - s).days + 1)]
    elif args.date:
        dates = [args.date]
    else:
        dates = ["2025-01-01"]
        print("⚠️  Aucune date spécifiée, utilisation de 2025-01-01")

    print(f"\n{'='*70}")
    print(f"📅 {len(dates)} date(s): {dates[0]} → {dates[-1]}")
    print(f"   Output:    {args.output}")
    print(f"   ZSTD lvl:  {ZSTD_LEVEL}")
    print(f"{'='*70}")

    # ── Producer/consumer: download en arrière-plan, process au fur et à mesure ──
    con = duckdb.connect()
    con.execute("SET memory_limit = '8GB'")
    con.execute("SET temp_directory = '/tmp/duckdb_noaa'")
    con.execute("SET preserve_insertion_order = false")

    dl_dir = args.download_dir
    os.makedirs(args.output, exist_ok=True)
    os.makedirs(dl_dir, exist_ok=True)

    total_pos = 0
    n_processed = 0
    n_skipped = 0
    n_total = len(dates)
    t_start = time.time()
    dl_error = threading.Event()
    file_queue = queue.Queue(maxsize=3)  # buffer de 3 fichiers pre-downloadés

    def download_worker():
        """Thread: télécharge en parallèle (4 connexions), alimente la queue."""
        dl_workers = args.dl_workers
        sorted_dates = sorted(dates)

        def _dl_one(date_str):
            year = date_str[:4]

            # Si déjà traité (Parquet valide existe) → skip le download
            positions_file = os.path.join(
                args.output, "positions", f"date={date_str}", "positions.parquet"
            )
            if os.path.exists(positions_file) and os.path.getsize(positions_file) > 1000:
                try:
                    # Vérifie que le Parquet est valide
                    with duckdb.connect() as c:
                        c.execute(
                            f"SELECT COUNT(*) FROM read_parquet('{positions_file}')"
                        ).fetchone()
                    return date_str, positions_file, "already_done"
                except Exception:
                    # Fichier corrompu → supprimer et re-download
                    os.remove(positions_file)

            zst_path = os.path.join(dl_dir, f"ais-{date_str}.csv.zst")
            if os.path.exists(zst_path) and os.path.getsize(zst_path) >= MIN_ZST_SIZE:
                return date_str, zst_path, None
            alt = os.path.join(PROJECT_ROOT, f"ais-{date_str}.csv.zst")
            if os.path.exists(alt) and os.path.getsize(alt) >= MIN_ZST_SIZE:
                return date_str, alt, None
            # Supprimer un fichier trop petit (download corrompu précédent)
            for p in [zst_path, alt]:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            if args.no_download:
                return date_str, None, "introuvable (--no-download)"
            url = f"{NOAA_BASE}/csv{year}/ais-{date_str}.csv.zst"
            try:
                urllib.request.urlretrieve(url, zst_path)
                size_mb = os.path.getsize(zst_path) / 1e6
                print(f"   📥 {date_str} ({size_mb:.0f} MB)")
                return date_str, zst_path, None
            except Exception as e:
                return date_str, None, str(e)

        with ThreadPoolExecutor(max_workers=dl_workers) as pool:
            # Soumettre tous les downloads, récupérer dans l'ordre
            futures = {pool.submit(_dl_one, d): d for d in sorted_dates}
            for future in as_completed(futures):
                date_str, path, err = future.result()
                if err == "already_done":
                    # Déjà traité — signaler au consumer sans passer par la queue
                    file_queue.put((date_str, path, True))
                elif err:
                    print(f"   ❌ {date_str}: {err}")
                    dl_error.set()
                    file_queue.put(None)
                    return
                else:
                    file_queue.put((date_str, path, False))

        file_queue.put(None)  # sentinelle

    dl_thread = threading.Thread(target=download_worker, daemon=True)
    dl_thread.start()

    # ── Consumer: traite un fichier, pendant que le suivant se télécharge ──
    while True:
        item = file_queue.get()
        if item is None:
            break
        date_str, zst_path, already_done = item

        i = n_processed + n_skipped + 1

        if already_done:
            n = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{zst_path}')"
            ).fetchone()[0]
            total_pos += n
            n_skipped += 1
            continue

        # Vérifier si déjà traité (skip si Parquet valide existe)
        positions_file = os.path.join(
            args.output, "positions", f"date={date_str}", "positions.parquet"
        )
        if os.path.exists(positions_file) and os.path.getsize(positions_file) > 1000:
            try:
                n = con.execute(
                    f"SELECT COUNT(*) FROM read_parquet('{positions_file}')"
                ).fetchone()[0]
                total_pos += n
                n_skipped += 1
                print(f"   ♻️  {date_str}: déjà traité ({n:,} lignes) [{i}/{n_total}]")
                try:
                    os.remove(zst_path)
                except OSError:
                    pass
                continue
            except Exception:
                # Fichier corrompu → re-traiter
                os.remove(positions_file)

        # Transformer
        n_pos, n_vess = process_date(con, zst_path, date_str, args.output)
        total_pos += n_pos
        n_processed += 1

        # Merge vessels
        n_total_vess = merge_vessels(con, args.output)

        # Nettoyer .csv.zst
        try:
            os.remove(zst_path)
        except OSError:
            pass

        # Progression
        elapsed = time.time() - t_start
        done = n_processed + n_skipped
        remaining = n_total - done
        eta = (elapsed / done * remaining) if done > 0 else 0
        queue_size = file_queue.qsize()
        print(f"      [{done}/{n_total}] {total_pos:,} positions, "
              f"{n_total_vess:,} vessels | {elapsed/60:.0f}min (+{queue_size} en attente), "
              f"~{eta/60:.0f}min restantes")

    dl_thread.join()

    con.close()

    # ── Stats finales ───────────────────────────────────────────────────
    elapsed = time.time() - t_start
    print(f"\n{'='*70}")
    print(f"✅ {n_processed} jours traités, {n_skipped} skip, {n_processed + n_skipped}/{len(dates)}")
    print(f"   Durée: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"   positions: {total_pos:,} lignes")

    con2 = duckdb.connect()
    show_schema(con2, args.output)
    con2.close()

    show_file_sizes(args.output)


if __name__ == "__main__":
    main()
