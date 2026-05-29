import boto3
import orjson
import zstandard as zstd
import pyarrow as pa
import pyarrow.parquet as pq
import os
import time
import shutil
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor
from configuration import *
import argparse
from datetime import datetime, timezone, timedelta

# ── Schéma Parquet cible ──────────────────────────────────────────────────────
SCHEMA = pa.schema([
    pa.field("message_type",        pa.string()),
    pa.field("mmsi",                pa.int64()),
    pa.field("ts",                  pa.timestamp("us", tz="UTC")),
    pa.field("lat",                 pa.float64()),
    pa.field("lon",                 pa.float64()),
    pa.field("received_at",         pa.timestamp("us", tz="UTC")),
    pa.field("source_listener",     pa.string()),
    pa.field("sog",                 pa.float64()),
    pa.field("cog",                 pa.float64()),
    pa.field("true_heading",        pa.int32()),
    pa.field("navigational_status", pa.int32()),
    pa.field("rate_of_turn",        pa.int32()),
    pa.field("message_id",          pa.int32()),
    pa.field("position_accuracy",   pa.bool_()),
    pa.field("raim",                pa.bool_()),
    pa.field("valid",               pa.bool_()),
    pa.field("name",                pa.string()),
    pa.field("call_sign",           pa.string()),
    pa.field("imo_number",          pa.int64()),
    pa.field("ship_type",           pa.int32()),
    pa.field("ais_version",         pa.int32()),
    pa.field("length",              pa.float64()),
    pa.field("width",               pa.float64()),
    pa.field("dimension_a",         pa.float64()),
    pa.field("dimension_b",         pa.float64()),
    pa.field("dimension_c",         pa.float64()),
    pa.field("dimension_d",         pa.float64()),
    pa.field("max_static_draught",  pa.float64()),
    pa.field("destination",         pa.string()),
    pa.field("eta",                 pa.timestamp("us")),
    pa.field("dte",                 pa.bool_()),
    pa.field("fix_type",            pa.int32()),
    pa.field("type_of_aton",        pa.int32()),
    pa.field("off_position",        pa.bool_()),
    pa.field("virtual_aton",        pa.bool_()),
    pa.field("raw_message",         pa.string()),
])

POSITION_TYPES = frozenset({
    'PositionReport', 'ExtendedClassBPositionReport',
    'StandardClassBPositionReport', 'LongRangeAisBroadcast',
})

BUCKET_SILVER = BUCKET_RAW


# ── Helpers de Parsing (Inchangés) ───────────────────────────────────────────
def parse_ts(raw: str | None) -> datetime | None:
    if not raw: return None
    try:
        cleaned = raw.replace(' +0000 UTC', '+00:00')
        parts = cleaned.split(' ')
        if len(parts) >= 2:
            hour = int(parts[1].split(':')[0])
            if hour > 23:
                parts[1] = '00:' + ':'.join(parts[1].split(':')[1:])
                cleaned = ' '.join(parts)
        return datetime.fromisoformat(cleaned)
    except Exception: return None

def parse_eta(eta: dict | None) -> datetime | None:
    if not eta: return None
    try:
        month = int(eta.get('Month', 0))
        day   = int(eta.get('Day',   0))
        if month < 1 or month > 12 or day < 1: return None
        max_days = {1:31,2:29,3:31,4:30,5:31,6:30,7:31,8:31,9:30,10:31,11:30,12:31}
        if day > max_days.get(month, 31): return None
        year   = int(eta.get('Year')   or 2024)
        hour   = min(int(eta.get('Hour')   or 0), 23)
        minute = min(int(eta.get('Minute') or 0), 59)
        return datetime(year, month, day, hour, minute, 0)
    except (ValueError, TypeError): return None

def extract_record(data: dict) -> dict:
    mtype    = data.get('message_type', '')
    metadata = data.get('metadata', {})
    message  = data.get('message', {})
    sub      = message.get(mtype, {})

    def dim(source: dict, key: str) -> float | None:
        v = source.get('Dimension', {})
        return v.get(key) if v else None

    def safe_add(a, b) -> float | None:
        if a is None and b is None: return None
        return (a or 0.0) + (b or 0.0)

    if   mtype == 'ShipStaticData':          dim_src = sub
    elif mtype == 'StaticDataReport':        dim_src = message.get('StaticDataReport', {}).get('ReportB', {})
    elif mtype == 'AidsToNavigationReport':  dim_src = sub
    else:                                    dim_src = {}

    da, db, dc, dd = dim(dim_src,'A'), dim(dim_src,'B'), dim(dim_src,'C'), dim(dim_src,'D')

    report_a = message.get('StaticDataReport', {}).get('ReportA', {})
    report_b = message.get('StaticDataReport', {}).get('ReportB', {})

    if   mtype == 'ShipStaticData':         name = sub.get('Name')      or metadata.get('ShipName')
    elif mtype == 'StaticDataReport':       name = report_a.get('Name') or metadata.get('ShipName')
    elif mtype == 'AidsToNavigationReport': name = sub.get('Name')      or metadata.get('ShipName')
    else:                                   name = metadata.get('ShipName')

    return {
        'message_type':        mtype,
        'mmsi':                metadata.get('MMSI'),
        'ts':                  parse_ts(metadata.get('time_utc')),
        'lat':                 metadata.get('latitude')  or metadata.get('Latitude'),
        'lon':                 metadata.get('longitude') or metadata.get('Longitude'),
        'received_at':         datetime.fromisoformat(data['received_at']) if data.get('received_at') else None,
        'source_listener':     data.get('listener_id'),
        'sog':                 sub.get('Sog'),
        'cog':                 sub.get('Cog'),
        'true_heading':        sub.get('TrueHeading'),
        'navigational_status': sub.get('NavigationalStatus'),
        'rate_of_turn':        sub.get('RateOfTurn'),
        'message_id':          sub.get('MessageID'),
        'position_accuracy':   sub.get('PositionAccuracy'),
        'raim':                sub.get('Raim'),
        'valid':               sub.get('Valid'),
        'name':                name,
        'call_sign':           sub.get('CallSign')  or report_b.get('CallSign'),
        'imo_number':          sub.get('ImoNumber') or report_a.get('ImoNumber'),
        'ship_type':           sub.get('Type')      or report_b.get('ShipType'),
        'ais_version':         sub.get('AisVersion'),
        'length':              safe_add(da, db),
        'width':               safe_add(dc, dd),
        'dimension_a': da, 'dimension_b': db, 'dimension_c': dc, 'dimension_d': dd,
        'max_static_draught':  sub.get('MaximumStaticDraught'),
        'destination':         sub.get('Destination') or report_a.get('Destination'),
        'eta':                 parse_eta(sub.get('Eta')),
        'dte':                 sub.get('Dte') or report_a.get('Dte'),
        'fix_type':            sub.get('FixType'),
        'type_of_aton':        sub.get('Type') if mtype == 'AidsToNavigationReport' else None,
        'off_position':        sub.get('OffPosition'),
        'virtual_aton':        sub.get('VirtualAidsToNavigation'),
        'raw_message':         orjson.dumps(message).decode() if mtype not in POSITION_TYPES else None,
    }


# ── Worker Optimisé ──────────────────────────────────────────────────────────
def _worker(args: tuple) -> tuple[int, int, str]:
    """
    Chaque worker écrit UN SEUL fichier Parquet plat temporaire contenant 
    TOUS les types de messages mixés.
    """
    s3_cfg, s3_keys, tmp_worker_dir, worker_id = args

    s3 = boto3.client(
        's3',
        endpoint_url=s3_cfg['endpoint_url'],
        aws_access_key_id=s3_cfg['access_key'],
        aws_secret_access_key=s3_cfg['secret_key'],
        region_name=s3_cfg['region'],
    )

    BATCH_SIZE = 5000
    buffer = []
    total_rows = 0
    parse_errors = 0

    # Création d'un unique fichier Parquet par worker dans un dossier temporaire
    out_path = os.path.join(tmp_worker_dir, f"raw_worker_{worker_id:02d}.parquet")
    writer = pq.ParquetWriter(out_path, schema=SCHEMA, compression='zstd')

    def flush_buffer():
        if buffer:
            writer.write_batch(pa.RecordBatch.from_pylist(buffer, schema=SCHEMA))
            buffer.clear()

    try:
        for s3_key in s3_keys:
            try:
                obj      = s3.get_object(Bucket=s3_cfg['bucket'], Key=s3_key)
                raw_zst  = obj['Body'].read()
                raw_json = zstd.ZstdDecompressor().decompress(raw_zst)
            except Exception:
                parse_errors += 1
                continue

            for line in raw_json.split(b'\n'):
                if not line.strip(): continue
                try:
                    data = orjson.loads(line)
                    if not data.get('metadata', {}).get('MMSI'): continue
                    rec = extract_record(data)
                    
                    if not rec['message_type']:
                        rec['message_type'] = 'UnknownMessage'

                    buffer.append(rec)
                    total_rows += 1

                    if len(buffer) >= BATCH_SIZE:
                        flush_buffer()
                except Exception:
                    parse_errors += 1

        flush_buffer()

    finally:
        writer.close()

    return (total_rows, parse_errors, out_path)


# ── Orchestration Finale (Extraction + Tri global + Fusion) ───────────────────
def run_converter(target_date: datetime):
    start = time.time()
    
    prefix = f"raw/year={target_date.year}/month={target_date.month:02d}/day={target_date.day:02d}/"
    output_dir = f"silver/year={target_date.year}/month={target_date.month:02d}/day={target_date.day:02d}"
    tmp_worker_dir = f"tmp_workers_day_{target_date.day:02d}"

    print(f"📅 Traitement de la date : {target_date.strftime('%Y-%m-%d')}")
    print(f"🔍 Recherche du préfixe S3 : {prefix}")

    s3_cfg = {
        'endpoint_url': OVH_ENDPOINT,
        'access_key':   OVH_ACCESS_KEY,
        'secret_key':   OVH_SECRET_KEY,
        'region':       OVH_REGION,
        'bucket':       BUCKET_RAW,
    }

    s3 = boto3.client(
        's3',
        endpoint_url=OVH_ENDPOINT,
        aws_access_key_id=OVH_ACCESS_KEY,
        aws_secret_access_key=OVH_SECRET_KEY,
        region_name=OVH_REGION,
    )

    # ── 1. Lister les fichiers bruts ──────────────────────────────────────────
    paginator = s3.get_paginator('list_objects_v2')
    keys = [
        obj['Key']
        for page in paginator.paginate(Bucket=BUCKET_RAW, Prefix=prefix)
        for obj in page.get('Contents', [])
    ]
    print(f"🔄 {len(keys)} fichiers raw à convertir")
    if not keys: 
        print("⚠️ Aucun fichier trouvé pour cette date. Fin du script.")
        return

    n_workers = min(os.cpu_count() or 4, 8)
    print(f"⚙️  Orchestration sur {n_workers} processus")

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(tmp_worker_dir, exist_ok=True)

    # Dispatch round-robin
    batches     = [keys[i::n_workers] for i in range(n_workers)]
    worker_args = [(s3_cfg, batch, tmp_worker_dir, worker_id)
                   for worker_id, batch in enumerate(batches) if batch]

    # ── 2. Phase 1 : Lecture parallèle ────────────────────────────────────────
    total_rows, total_errors = 0, 0
    worker_files = []
    
    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        for worker_id, (rows, errs, path) in enumerate(pool.map(_worker, worker_args)):
            total_rows   += rows
            total_errors += errs
            worker_files.append(path)
            print(f"  worker {worker_id:02d} — {rows:,} lignes traitées")

    print(f"⏱️  Phase 1 (Parsing) terminée en {time.time()-start:.1f}s")

# ── 3. Phase 2 : Consolidation Globale, Dédoublonnage et Tri SANS RAM (Via DuckDB Disque) ───
    print("💎 Phase 2 : Dédoublonnage et Indexation Parquet optimisés (Spill-to-Disk)...")
    
    final_file_path = os.path.join(output_dir, "messages_consolidated.parquet")
    print(f"  └─ Préparation de l'écriture : {final_file_path}")
    
    import duckdb as ddb
    
    # On ouvre une connexion DuckDB persistante
    ctx = ddb.connect("tmp_consolidation.db")
    
    # --- OPTIMISATIONS MÉMOIRE ---
    # On bride la RAM DuckDB pour laisser de la place au système et éviter le OOM Killer
    ctx.execute("SET memory_limit='10GB';")
    # On définit explicitement un dossier temporaire pour le spill-to-disk
    duckdb_tmp = os.path.join(tmp_worker_dir, "duckdb_temp")
    os.makedirs(duckdb_tmp, exist_ok=True)
    ctx.execute(f"SET temp_directory='{duckdb_tmp}';")
    # Désactiver l'ordre d'insertion peut accélérer les tris/dédoublonnages
    ctx.execute("SET preserve_insertion_order=false;")
    # -----------------------------
    
    # On pointe directement vers les fichiers Parquet temporaires générés par les workers
    workers_pattern = os.path.join(tmp_worker_dir, "*.parquet")
    
    # La magie SQL : On dédoublonne avec le QUALIFY, on trie avec le ORDER BY, 
    # et on exporte DIRECTEMENT en Parquet en un seul flux continu (Streaming)
    query = f"""
        COPY (
            SELECT * FROM '{workers_pattern}'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY mmsi, ts, message_type 
                ORDER BY received_at ASC
            ) = 1
            ORDER BY message_type ASC, mmsi ASC, ts ASC
        ) TO '{final_file_path}' (FORMAT 'PARQUET', COMPRESSION 'ZSTD', ROW_GROUP_SIZE 100000);
    """
    
    # Exécution du pipeline de streaming
    ctx.execute(query)
    
    # Récupération rapide du compte final pour tes logs sans charger les données
    rows_after_dedup = ctx.execute(f"SELECT COUNT(*) FROM '{final_file_path}'").fetchone()[0]
    
    print(f"  └─ Lignes avant (brutes) : {total_rows:,} | Lignes après (uniques) : {rows_after_dedup:,} ({total_rows - rows_after_dedup:,} doublons supprimés)")

    # Nettoyage de la base temporaire DuckDB et des dossiers workers
    ctx.close()
    if os.path.exists("tmp_consolidation.db"):
        os.remove("tmp_consolidation.db")
    shutil.rmtree(tmp_worker_dir)

    print(f"✅ Opération terminée avec succès en {time.time()-start:.1f}s !")
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convertisseur AIS Raw to Silver Parquet")
    parser.add_argument(
        "--date", 
        help="Date à traiter au format YYYY-MM-DD. Si absent, traite la date d'HIER.",
        type=str,
        default=None
    )
    args = parser.parse_args()

    if args.date:
        # Si une date est fournie, on la parse
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print("❌ Format de date invalide. Utilisez YYYY-MM-DD (ex: 2026-05-26)")
            exit(1)
    else:
        # Par défaut : Aujourd'hui - 1 jour (Hier)
        target_date = datetime.now(timezone.utc) - timedelta(days=1)

    run_converter(target_date)