"""Phase 2-3: Local transform — consolidate + derive + vessels + port_calls + port_congestion."""

import os
import re
import time
import duckdb
from config import (
    S3_DATA_PREFIX, BUCKET_PUBLIC, PORTS_PARQUET,
    s3_client, load_sql, run_sql,
)
from port_calls import detect_port_calls


def local_transform(target_date, work_dir_path, output_dir_path, tables=None):
    """
    Consolidate + Derive locally. No DuckLake, no S3 — pure local DuckDB.
    If tables is specified, only those tables are generated.
    Returns stats dict with row counts per table.
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
        # ── Consolidate (messages / silver) ────────────────────────────
        if want_messages:
            n_raw = len([f for f in os.listdir(work_dir_path)
                         if f.endswith('.ndjson.zst')])
            print(f"   📦 Consolidation: {n_raw} fichiers NDJSON → Parquet...")
            run_sql(con, load_sql('01_consolidate.sql'), {
                'raw_path': raw_glob,
                'output_path': silver_file,
            })
            stats['messages'] = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{silver_file}')"
            ).fetchone()[0]
            print(f"   ✅ messages: {stats['messages']:,} lignes "
                  f"({time.time() - t0:.1f}s)")
        else:
            if not os.path.exists(silver_file):
                print("   📥 Téléchargement silver existant depuis S3...")
                s3 = s3_client()
                y, m, d = (target_date.year,
                           f"{target_date.month:02d}",
                           f"{target_date.day:02d}")
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

        # ── Early return check ─────────────────────────────────────────
        want_gold = all_tables or bool(
            (tables or set()) & {'vessels_positions', 'vessel_tracks',
                                 'base_stations', 'aids_to_navigation'}
        )
        want_extra = all_tables or bool(
            (tables or set()) & {'vessels', 'port_calls', 'port_congestion'}
        )
        if not want_gold and not want_extra:
            return stats

        # ── Derive gold tables ─────────────────────────────────────────
        if want_gold:
            print("   🏗️  Dérivation gold...")
            t1 = time.time()

        y, m, d = (target_date.year,
                   f"{target_date.month:02d}",
                   f"{target_date.day:02d}")
        date_str = target_date.strftime('%Y-%m-%d')

        vp_dir = os.path.join(gold_dir, 'vessels_positions',
                              f'year={y}', f'month={m}', f'day={d}')
        vt_dir = os.path.join(gold_dir, 'vessel_tracks', f'date={date_str}')
        bs_dir = os.path.join(gold_dir, 'base_stations',
                              f'year={y}', f'month={m}', f'day={d}')
        an_dir = os.path.join(gold_dir, 'aids_to_navigation',
                              f'year={y}', f'month={m}', f'day={d}')

        derive_tables = (tables if not all_tables
                         else {'vessels_positions', 'vessel_tracks',
                               'base_stations', 'aids_to_navigation'})

        for t, dpath in [('vessels_positions', vp_dir),
                         ('vessel_tracks', vt_dir),
                         ('base_stations', bs_dir),
                         ('aids_to_navigation', an_dir)]:
            if t in derive_tables:
                os.makedirs(dpath, exist_ok=True)

        derive_sql = load_sql('02_derive.sql')
        blocks = re.split(r'\n(?=-- \d+\. )', derive_sql)
        selected_blocks = [blocks[0]]
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
                p = os.path.join(gold_dir, table, f'date={date_str}',
                                 f'{table}.parquet')
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
            print(f"   ✅ Gold ({time.time() - t1:.1f}s): "
                  + ', '.join(f'{k}={v:,}' for k, v in stats.items()
                              if k != 'messages'))

        # ── Vessels ────────────────────────────────────────────────────
        want_vessels = all_tables or 'vessels' in (tables or set())
        if want_vessels:
            print("   🚢 Vessels...")
            t2 = time.time()
            vessels_dir = os.path.join(gold_dir, 'vessels')
            os.makedirs(vessels_dir, exist_ok=True)
            vessels_file = os.path.join(vessels_dir, 'vessels.parquet')

            existing_vessels_path = os.path.join(gold_dir,
                                                  'vessels_existing.parquet')
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
                try:
                    os.remove(existing_vessels_path)
                except OSError:
                    pass
            else:
                run_sql(con, load_sql('03_vessels_from_scratch.sql'), {
                    'silver_path': silver_file,
                    'output_path': vessels_file,
                })

            stats['vessels'] = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{vessels_file}')"
            ).fetchone()[0]
            print(f"   ✅ vessels: {stats['vessels']:,} navires "
                  f"({time.time() - t2:.1f}s)")

        # ── Port Calls ─────────────────────────────────────────────────
        want_port_calls = all_tables or 'port_calls' in (tables or set())
        want_port_congestion = all_tables or 'port_congestion' in (tables or set())

        if want_port_calls or want_port_congestion:
            pc_dir = os.path.join(gold_dir, 'port_calls')
            os.makedirs(pc_dir, exist_ok=True)
            existing_pc_path = os.path.join(pc_dir, 'port_calls_existing.parquet')
            existing_key = f"{S3_DATA_PREFIX}/gold/port_calls/port_calls.parquet"

            if want_port_calls:
                has_existing_pc = False
                try:
                    s3_client().download_file(
                        BUCKET_PUBLIC, existing_key, existing_pc_path
                    )
                    has_existing_pc = True
                    print("   📥 Port calls existants téléchargés pour merge")
                except Exception:
                    print("   🆕 Premier run port_calls")

                print("   🚢 Détection port calls...")
                t3 = time.time()
                port_calls_file, n_pc = detect_port_calls(
                    con, silver_file, gold_dir,
                    ports_path=PORTS_PARQUET,
                    existing_path=existing_pc_path,
                    has_existing=has_existing_pc,
                )
                stats['port_calls'] = n_pc
                print(f"   ✅ port_calls: {n_pc:,} ({time.time() - t3:.1f}s)")

                try:
                    os.remove(existing_pc_path)
                except OSError:
                    pass
            else:
                port_calls_file = os.path.join(pc_dir, 'port_calls.parquet')
                if os.path.exists(existing_pc_path):
                    port_calls_file = existing_pc_path
                else:
                    try:
                        s3_client().download_file(
                            BUCKET_PUBLIC, existing_key, port_calls_file
                        )
                        print("   📥 Port calls téléchargés pour congestion")
                    except Exception:
                        print("   ⚠️ Pas de port_calls, congestion ignorée")
                        want_port_congestion = False
        else:
            want_port_congestion = False

        # ── Port Congestion ─────────────────────────────────────────────
        if want_port_congestion:
            print("   📊 Port congestion...")
            t4 = time.time()
            pc_cong_dir = os.path.join(gold_dir, 'port_congestion')
            os.makedirs(pc_cong_dir, exist_ok=True)
            pc_cong_file = os.path.join(pc_cong_dir, 'port_congestion.parquet')
            run_sql(con, load_sql('04_port_congestion.sql'), {
                'port_calls_path': port_calls_file,
                'output_path': pc_cong_file,
            })
            stats['port_congestion'] = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{pc_cong_file}')"
            ).fetchone()[0]
            print(f"   ✅ port_congestion: {stats['port_congestion']:,} lignes "
                  f"({time.time() - t4:.1f}s)")
    finally:
        con.close()

    return stats
