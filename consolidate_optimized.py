import boto3
import orjson
import zstandard as zstd
import pyarrow as pa
import pyarrow.parquet as pq
import os
import time
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed
from configuration import *

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


# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        cleaned = raw.replace(' +0000 UTC', '+00:00')
        parts = cleaned.split(' ')
        if len(parts) >= 2:
            hour = int(parts[1].split(':')[0])
            if hour > 23:
                parts[1] = '00:' + ':'.join(parts[1].split(':')[1:])
                cleaned = ' '.join(parts)
        return datetime.fromisoformat(cleaned)
    except Exception:
        return None


def parse_eta(eta: dict | None) -> datetime | None:
    if not eta:
        return None
    try:
        month = int(eta.get('Month', 0))
        day   = int(eta.get('Day',   0))
        if month < 1 or month > 12 or day < 1:
            return None
        max_days = {1:31,2:29,3:31,4:30,5:31,6:30,7:31,8:31,9:30,10:31,11:30,12:31}
        if day > max_days.get(month, 31):
            return None
        year   = int(eta.get('Year')   or 2024)
        hour   = min(int(eta.get('Hour')   or 0), 23)
        minute = min(int(eta.get('Minute') or 0), 59)
        return datetime(year, month, day, hour, minute, 0)
    except (ValueError, TypeError):
        return None


def extract_record(data: dict) -> dict:
    mtype    = data.get('message_type', '')
    metadata = data.get('metadata', {})
    message  = data.get('message', {})
    sub      = message.get(mtype, {})

    def dim(source: dict, key: str) -> float | None:
        v = source.get('Dimension', {})
        return v.get(key) if v else None

    def safe_add(a, b) -> float | None:
        if a is None and b is None:
            return None
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


# ── Worker (tourne dans un process séparé, pas de GIL partagé) ───────────────
def _worker(args: tuple) -> tuple[int, int]:
    """
    Download → parse → write Parquet local partitionné par message_type.
    Retourne (lignes_écrites, erreurs_parsing).

    Chaque worker est un process indépendant :
    - pas de GIL partagé → parsing CPU vraiment parallèle
    - pas de concat_tables central → mémoire bornée par fichier
    """
    s3_cfg, s3_key, tmpdir, idx, worker_id = args

    s3 = boto3.client(
        's3',
        endpoint_url=s3_cfg['endpoint_url'],
        aws_access_key_id=s3_cfg['access_key'],
        aws_secret_access_key=s3_cfg['secret_key'],
        region_name=s3_cfg['region'],
    )

    # ── Download + décompression ──────────────────────────────────────────────
    try:
        obj      = s3.get_object(Bucket=s3_cfg['bucket'], Key=s3_key)
        raw_zst  = obj['Body'].read()
        raw_json = zstd.ZstdDecompressor().decompress(raw_zst)
    except Exception as e:
        return (0, 1)

    # ── Parsing — groupé par message_type pour écriture directe ──────────────
    by_type: dict[str, list] = {}
    parse_errors = 0
    for line in raw_json.split(b'\n'):
        if not line.strip():
            continue
        try:
            data = orjson.loads(line)
            if not data.get('metadata', {}).get('MMSI'):
                continue
            rec   = extract_record(data)
            mtype = rec['message_type'] or 'unknown'
            if mtype not in by_type:
                by_type[mtype] = []
            by_type[mtype].append(rec)
        except Exception:
            parse_errors += 1

    if not by_type:
        return (0, parse_errors)

    # ── Écriture Parquet locale — un fichier par message_type ────────────────
    total = 0
    for mtype, records in by_type.items():
        out_dir = os.path.join(tmpdir, f"message_type={mtype}")
        os.makedirs(out_dir, exist_ok=True)
        table = pa.Table.from_pylist(records, schema=SCHEMA)
        pq.write_table(
            table,
            os.path.join(out_dir, f"worker_{worker_id:02d}.parquet"),
            compression='zstd',
        )
        total += len(records)

    return (total, parse_errors)





# ── Orchestration ─────────────────────────────────────────────────────────────
def run_converter():
    start = time.time()
    today = datetime.now(timezone.utc)
    prefix       = f"raw/year={today.year}/month={today.month:02d}/day={today.day:02d}/"
    s3_out_prefix = f"silver/year={today.year}/month={today.month:02d}/day={today.day:02d}"

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
    print(f"🔄 {len(keys)} fichiers à convertir")
    if not keys:
        return

    # ── 2. Download + parse + write Parquet local (ProcessPoolExecutor) ───────
    # Un process par CPU : pas de GIL, pas de concat_tables central.
    # Chaque process écrit ses propres fichiers Parquet dans silver/...
    n_workers = os.cpu_count() or 4
    print(f"⚙️  {n_workers} processus (CPU count)")

    # Créer le dossier silver local
    output_dir = s3_out_prefix
    os.makedirs(output_dir, exist_ok=True)

    worker_args = [(s3_cfg, key, output_dir, i, i % n_workers) for i, key in enumerate(keys)]

    total_rows, total_errors = 0, 0
    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        # chunksize : chaque process reçoit 10 fichiers à la fois
        # → réduit l'overhead de scheduling inter-process
        for i, (rows, errs) in enumerate(
            pool.map(_worker, worker_args, chunksize=20)
        ):
            total_rows   += rows
            total_errors += errs
            if (i + 1) % 100 == 0:
                elapsed = time.time() - start
                rate = (i + 1) / elapsed
                eta  = (len(keys) - i - 1) / rate
                print(f"  {i+1}/{len(keys)} — {rate:.0f} fichiers/s — ETA {eta:.0f}s")

    print(f"⏱️  Extraction + écriture locale : {time.time()-start:.1f}s "
          f"— {total_rows:,} lignes ({total_errors} erreurs parsing)")

    print(f"✅ Conversion terminée en {time.time()-start:.1f}s")
    print(f"   → Fichiers Parquet sauvegardés dans {output_dir}/message_type=<type>/")


if __name__ == "__main__":
    run_converter()