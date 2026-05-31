#!/usr/bin/env python3
import duckdb
import boto3
import os
import glob
import argparse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from configuration import *

POSITION_TYPES = frozenset({
    'PositionReport', 'ExtendedClassBPositionReport',
    'StandardClassBPositionReport', 'LongRangeAisBroadcast',
})

PARTITIONED_TABLES = {
    'vessels_positions': {
        'filter_col': 'message_type',
        'filter': f"message_type IN ({','.join(f"'{t}'" for t in POSITION_TYPES)})",
        'projection': 'message_type, mmsi, ts, lat, lon, received_at, source_listener, sog, cog, true_heading, navigational_status, rate_of_turn, message_id, position_accuracy, raim, valid, year, month, day',
    },
    'base_stations': {
        'filter_col': 'message_type',
        'filter': "message_type = 'BaseStationReport'",
        'projection': 'mmsi, ts, lat, lon, received_at, source_listener, message_id, raim, year, month, day',
    },
    'aids_to_navigation': {
        'filter_col': 'message_type',
        'filter': "message_type = 'AidsToNavigationReport'",
        'projection': 'mmsi, name, type_of_aton, ts, lat, lon, dimension_a, dimension_b, dimension_c, dimension_d, off_position, virtual_aton, raim, received_at, source_listener, year, month, day',
    },
}


def extract_derived_tables(con, target_date, local_silver):
    derived = []
    silver_glob = os.path.join(local_silver, "*.parquet")
    if not os.path.exists(local_silver):
        return derived

    for table_name, cfg in PARTITIONED_TABLES.items():
        out_dir = f"gold/{table_name}/year={target_date.year}/month={target_date.month:02d}/day={target_date.day:02d}"
        os.makedirs(out_dir, exist_ok=True)
        out_file = os.path.join(out_dir, f"{table_name}.parquet")
        con.execute(f"""
            COPY (
                SELECT {cfg['projection']}
                FROM read_parquet('{silver_glob}', hive_partitioning=true)
                WHERE {cfg['filter']}
            ) TO '{out_file}' (FORMAT 'PARQUET', COMPRESSION 'ZSTD')
        """)
        count = con.execute(f"SELECT count(*) FROM '{out_file}'").fetchone()[0]
        derived.append((table_name, out_file, out_dir))
        print(f"   └─ {table_name}: {count:,} lignes → {out_file}")
    return derived


def build_vessels_reference(con, s3, force_rebuild=False):
    local_file = "gold/vessels/vessels.parquet"
    os.makedirs("gold/vessels", exist_ok=True)

    if not force_rebuild and os.path.exists(local_file):
        print(f"   └─ vessels.parquet existe déjà localement ({os.path.getsize(local_file)//1024} KB)")
        return local_file, False

    silver_glob = "silver/year=*/month=*/day=*/messages_consolidated.parquet"
    files = [f for f in glob.glob(silver_glob) if os.path.exists(f)]
    if not files:
        print("   ⚠️ Aucune donnée silver trouvée pour construire vessels")
        return None, False

    print(f"🏗️  Construction de la table vessels depuis {len(files)} fichier(s) silver...")
    files_list = ", ".join(f"'{f}'" for f in files)
    con.execute(f"""
        COPY (
            WITH ranked AS (
                SELECT mmsi, name, call_sign, imo_number, ship_type,
                       length, width, destination, ts AS last_seen_static
                FROM read_parquet(ARRAY[{files_list}])
                WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
                  AND name IS NOT NULL
                QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY ts DESC) = 1
            )
            SELECT * FROM ranked ORDER BY mmsi
        ) TO '{local_file}' (FORMAT 'PARQUET', COMPRESSION 'ZSTD')
    """)
    count = con.execute(f"SELECT count(*) FROM '{local_file}'").fetchone()[0]
    print(f"   ✅ vessels: {count:,} navires enregistrés")
    return local_file, True


def delete_partition(con, table_name, target_date):
    try:
        con.execute(f"DELETE FROM ais_lake.{table_name} WHERE year = {target_date.year} AND month = {target_date.month} AND day = {target_date.day}")
        return True
    except Exception as e:
        print(f"   ⚠️ DELETE sur {table_name} : {e}")
        return False


def publish_ducklake(target_date: datetime, force: bool = False, rebuild_vessels: bool = False):
    print(f"📅 Publication DuckLake pour la date : {target_date.strftime('%Y-%m-%d')}")

    # ── 1. Configuration des chemins ───────────────────────────────────
    local_silver = f"silver/year={target_date.year}/month={target_date.month:02d}/day={target_date.day:02d}"
    s3_silver_prefix = f"silver/year={target_date.year}/month={target_date.month:02d}/day={target_date.day:02d}"

    config = boto3.session.Config(
        s3={
            'max_concurrency': 20,
            'multipart_threshold': 8 * 1024 * 1024,
            'multipart_chunksize': 8 * 1024 * 1024,
        }
    )
    s3 = boto3.client(
        's3',
        endpoint_url=OVH_ENDPOINT,
        aws_access_key_id=OVH_ACCESS_KEY,
        aws_secret_access_key=OVH_SECRET_KEY,
        region_name=OVH_REGION,
        config=config,
    )

    # ── 2. Collecter les fichiers silver ──────────────────────────────
    upload_args = []
    if os.path.exists(local_silver):
        for root, _, files in os.walk(local_silver):
            for f in files:
                if not f.endswith(".parquet"):
                    continue
                local_path = os.path.join(root, f)
                rel_path = os.path.relpath(local_path, local_silver)
                s3_key = f"{s3_silver_prefix}/{rel_path}"
                upload_args.append((local_path, s3_key))

    if not upload_args:
        print(f"⚠️  Aucun fichier Parquet trouvé localement dans {local_silver}")
        if not force:
            return

    # ── 3. Télécharger ais.ducklake existant ──────────────────────────
    local_metadata_dir = "ducklake_metadata"
    local_metadata = os.path.join(local_metadata_dir, "ais.ducklake")
    local_data_path = os.path.abspath(os.path.join(local_metadata_dir, "data"))
    os.makedirs(local_data_path, exist_ok=True)
    if os.path.exists(local_metadata):
        os.remove(local_metadata)

    base_https = f"https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net"
    is_new = False
    print(f"📥 Tentative de récupération de ais.ducklake...")
    try:
        s3.download_file(BUCKET_PUBLIC, "ais.ducklake", local_metadata)
        print("   ✅ Récupéré via Boto3")
    except Exception as e:
        print(f"   ⚠️ Échec Boto3 ({e}), tentative via HTTPS direct...")
        try:
            import requests
            r = requests.get(f"{base_https}/ais.ducklake", timeout=10)
            if r.status_code == 200:
                with open(local_metadata, 'wb') as f:
                    f.write(r.content)
                print("   ✅ Récupéré via HTTPS direct")
            else:
                print(f"   🆕 Nouveau DuckLake (Fichier absent ou HTTP {r.status_code})")
                is_new = True
        except Exception as e2:
            print(f"   🆕 Nouveau DuckLake (Erreur : {e2})")
            is_new = True

    # ── 4. Configurer DuckDB ──────────────────────────────────────────
    con = duckdb.connect()
    con.execute("INSTALL httpfs; INSTALL ducklake; LOAD httpfs; LOAD ducklake;")
    con.execute(f"SET s3_endpoint='{OVH_ENDPOINT.replace('https://', '')}'")
    con.execute("SET s3_region='gra'")
    con.execute(f"SET s3_access_key_id='{OVH_ACCESS_KEY}'")
    con.execute(f"SET s3_secret_access_key='{OVH_SECRET_KEY}'")
    con.execute("SET s3_url_style='path'; SET s3_use_ssl=true")

    public_data_path = f"{base_https}/"

    # ── 5. Attacher DuckLake ──────────────────────────────────────────
    if is_new:
        con.execute(f"""
            ATTACH '{local_metadata}' AS ais_lake (
                TYPE ducklake, DATA_PATH '{public_data_path}',
                OVERRIDE_DATA_PATH true, AUTOMATIC_MIGRATION true
            )
        """)
        con.execute("DETACH ais_lake")
        print(f"💾 DATA_PATH public distant configuré : {public_data_path}")

    con.execute(f"""
        ATTACH '{local_metadata}' AS ais_lake (
            TYPE ducklake, DATA_PATH '{local_data_path}',
            OVERRIDE_DATA_PATH true
        )
    """)

    # ── 6. Créer les tables si premier run ────────────────────────────
    if is_new and upload_args:
        silver_glob_tables = os.path.join(local_silver, "*.parquet")
        con.execute(f"CREATE TABLE IF NOT EXISTS ais_lake.messages AS SELECT * FROM read_parquet('{silver_glob_tables}', hive_partitioning=true) WITH NO DATA")
        try:
            con.execute("ALTER TABLE ais_lake.messages SET PARTITIONED BY (year, month, day);")
        except Exception:
            pass
        print("🗂️  Table 'messages' initialisée")

        # Tables dérivées partitionnées
        for table_name, cfg in PARTITIONED_TABLES.items():
            con.execute(f"""
                CREATE TABLE IF NOT EXISTS ais_lake.{table_name} AS
                    SELECT {cfg['projection']} FROM read_parquet('{silver_glob_tables}', hive_partitioning=true) WITH NO DATA
            """)
            try:
                con.execute(f"ALTER TABLE ais_lake.{table_name} SET PARTITIONED BY (year, month, day);")
            except Exception:
                pass
            print(f"🗂️  Table '{table_name}' initialisée")

        # Table vessels (non partitionnée)
        con.execute("""
            CREATE TABLE IF NOT EXISTS ais_lake.vessels (
                mmsi BIGINT, name VARCHAR, call_sign VARCHAR,
                imo_number BIGINT, ship_type INTEGER,
                length DOUBLE, width DOUBLE, destination VARCHAR,
                last_seen_static TIMESTAMPTZ
            )
        """)
        print("🗂️  Table 'vessels' initialisée")

    # ── 7. Créer les tables dérivées si absentes du catalogue existant ─
    if not is_new:
        silver_glob_tables = os.path.join(local_silver, "*.parquet")
        for table_name, cfg in PARTITIONED_TABLES.items():
            try:
                con.execute(f"SELECT 1 FROM ais_lake.{table_name} LIMIT 0")
            except Exception:
                if upload_args and os.path.exists(local_silver):
                    con.execute(f"""
                        CREATE TABLE IF NOT EXISTS ais_lake.{table_name} AS
                            SELECT {cfg['projection']} FROM read_parquet('{silver_glob_tables}', hive_partitioning=true) WITH NO DATA
                    """)
                    try:
                        con.execute(f"ALTER TABLE ais_lake.{table_name} SET PARTITIONED BY (year, month, day);")
                    except Exception:
                        pass
                    print(f"🗂️  Table '{table_name}' créée (catalogue existant)")
        try:
            con.execute("SELECT 1 FROM ais_lake.vessels LIMIT 0")
        except Exception:
            con.execute("""
                CREATE TABLE IF NOT EXISTS ais_lake.vessels (
                    mmsi BIGINT, name VARCHAR, call_sign VARCHAR,
                    imo_number BIGINT, ship_type INTEGER,
                    length DOUBLE, width DOUBLE, destination VARCHAR,
                    last_seen_static TIMESTAMPTZ
                )
            """)
            print("🗂️  Table 'vessels' créée (catalogue existant)")

    # ── 8. Nettoyage FORCE pour toutes les tables partitionnées ───────
    if force and not is_new:
        print(f"🗑️  Mode FORCE : Nettoyage des données pour le {target_date.strftime('%Y-%m-%d')}")
        delete_partition(con, 'messages', target_date)
        for table_name in PARTITIONED_TABLES:
            delete_partition(con, table_name, target_date)
    elif force and is_new:
        print("✨ Mode FORCE sur nouveau catalogue : rien à nettoyer")

    # ── 9. Extraire les tables dérivées du silver consolidé ───────────
    derived_files = extract_derived_tables(con, target_date, local_silver)

    # ── 10. Construire la table vessels si demandé ────────────────────
    vessels_file, vessels_built = None, False
    if rebuild_vessels or not os.path.exists("gold/vessels/vessels.parquet"):
        vessels_file, vessels_built = build_vessels_reference(con, s3, force_rebuild=rebuild_vessels)

    # ── 11. Uploader TOUS les fichiers vers S3 ────────────────────────
    all_uploads = list(upload_args)
    for table_name, local_path, _ in derived_files:
        s3_key = os.path.relpath(local_path, "gold")
        all_uploads.append((local_path, f"gold/{s3_key}"))
    all_uploads.append(("gold/vessels/vessels.parquet", "gold/vessels/vessels.parquet"))

    if all_uploads:
        print(f"📤 Upload de {len(all_uploads)} fichier(s) vers S3...")
        def _upload_file(args):
            local_path, s3_key = args
            s3.upload_file(local_path, BUCKET_PUBLIC, s3_key, ExtraArgs={"ACL": "public-read"})
        with ThreadPoolExecutor(max_workers=32) as pool:
            list(pool.map(_upload_file, all_uploads))
        print("✅ Fichiers uploadés")

    # ── 12. Connaître les fichiers déjà enregistrés ───────────────────
    known_paths = {
        row[0]
        for row in con.execute(
            "SELECT path FROM __ducklake_metadata_ais_lake.ducklake_data_file"
        ).fetchall()
    }

    # ── 13. Enregistrer les fichiers messages ─────────────────────────
    to_register = [k for _, k in upload_args] if force else [
        k for _, k in upload_args if f"{base_https}/{k}" not in known_paths
    ]
    if to_register:
        print(f"📋 Enregistrement de {len(to_register)} fichier(s) dans 'messages'...")
        for s3_key in to_register:
            con.execute(f"CALL ducklake_add_data_files('ais_lake', 'messages', '{base_https}/{s3_key}', hive_partitioning=True)")

    # ── 14. Enregistrer les fichiers des tables dérivées ─────────────
    for table_name, _, local_dir in derived_files:
        s3_prefix = os.path.relpath(local_dir, "gold")
        s3_keys = []
        for root, _, files in os.walk(local_dir):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), local_dir)
                s3_keys.append(f"gold/{s3_prefix}/{rel}")

        to_reg = [k for k in s3_keys] if force else [
            k for k in s3_keys if f"{base_https}/{k}" not in known_paths
        ]
        if to_reg:
            print(f"📋 Enregistrement de {len(to_reg)} fichier(s) dans '{table_name}'...")
            for s3_key in to_reg:
                con.execute(f"CALL ducklake_add_data_files('ais_lake', '{table_name}', '{base_https}/{s3_key}', hive_partitioning=True)")

    # ── 15. Enregistrer le fichier vessels ────────────────────────────
    vessels_s3_key = "gold/vessels/vessels.parquet"
    vessels_url = f"{base_https}/{vessels_s3_key}"
    if vessels_url not in known_paths or force:
        if not os.path.exists("gold/vessels/vessels.parquet"):
            vessels_file, _ = build_vessels_reference(con, s3, force_rebuild=False)
        print(f"📋 Enregistrement de vessels...")
        con.execute(f"CALL ducklake_add_data_files('ais_lake', 'vessels', '{vessels_url}')")

    # ── 16. Fermer et uploader le catalogue ───────────────────────────
    con.close()
    print("💾 Catalogue DuckLake mis à jour localement")

    s3.upload_file(
        local_metadata, BUCKET_PUBLIC, "ais.ducklake",
        ExtraArgs={"ACL": "public-read"},
    )
    print("📤 ais.ducklake publié avec succès !")
    print(f"   → ATTACH 'https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net/ais.ducklake' AS ais (TYPE ducklake);")
    print(f"   → Tables disponibles : messages, vessels_positions, base_stations, aids_to_navigation, vessels")


def drop_table_from_catalog(table_name: str):
    local_metadata_dir = "ducklake_metadata"
    local_metadata = os.path.join(local_metadata_dir, "ais.ducklake")
    local_data_path = os.path.abspath(os.path.join(local_metadata_dir, "data"))
    os.makedirs(local_data_path, exist_ok=True)
    if os.path.exists(local_metadata):
        os.remove(local_metadata)

    base_https = f"https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net"
    s3 = boto3.client(
        's3',
        endpoint_url=OVH_ENDPOINT,
        aws_access_key_id=OVH_ACCESS_KEY,
        aws_secret_access_key=OVH_SECRET_KEY,
        region_name=OVH_REGION,
    )
    print(f"📥 Téléchargement de ais.ducklake...")
    try:
        s3.download_file(BUCKET_PUBLIC, "ais.ducklake", local_metadata)
    except Exception:
        import requests
        r = requests.get(f"{base_https}/ais.ducklake", timeout=10)
        r.raise_for_status()
        with open(local_metadata, 'wb') as f:
            f.write(r.content)

    con = duckdb.connect()
    con.execute("INSTALL ducklake; LOAD ducklake;")
    con.execute(f"""
        ATTACH '{local_metadata}' AS ais_lake (
            TYPE ducklake, DATA_PATH '{local_data_path}',
            OVERRIDE_DATA_PATH true
        )
    """)
    con.execute(f"DROP TABLE IF EXISTS ais_lake.{table_name}")
    con.close()

    s3.upload_file(local_metadata, BUCKET_PUBLIC, "ais.ducklake", ExtraArgs={"ACL": "public-read"})
    print(f"✅ Table '{table_name}' supprimée et catalogue republié")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Publication du catalogue DuckLake vers S3 public")
    parser.add_argument(
        "--date",
        help="Date au format YYYY-MM-DD. Par défaut : hier.",
        type=str, default=None
    )
    parser.add_argument(
        "--force",
        help="Force la re-publication (nettoie la date cible et ré-enregistre)",
        action="store_true"
    )
    parser.add_argument(
        "--rebuild-vessels",
        help="Reconstruit la table vessels depuis toutes les données silver",
        action="store_true"
    )
    parser.add_argument(
        "--drop-table",
        help="Supprime une table du catalogue DuckLake et republie",
        type=str, default=None
    )
    args = parser.parse_args()

    if args.drop_table:
        drop_table_from_catalog(args.drop_table)
        exit(0)

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print("❌ Format de date invalide. Utilisez YYYY-MM-DD (ex: 2026-05-26)")
            exit(1)
    else:
        target_date = datetime.now(timezone.utc) - timedelta(days=1)

    publish_ducklake(target_date, force=args.force, rebuild_vessels=args.rebuild_vessels)