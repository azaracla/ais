#!/usr/bin/env python3
import duckdb
import boto3
import os
import re
import argparse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from configuration import *


def publish_ducklake(target_date: datetime):
    print(f"📅 Publication DuckLake pour la date : {target_date.strftime('%Y-%m-%d')}")

    # ── 1. Configuration des chemins S3 en fonction de la date cible ─────────
    local_silver     = f"silver/year={target_date.year}/month={target_date.month:02d}/day={target_date.day:02d}"
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

    upload_args = []
    for root, _, files in os.walk(local_silver):
        for f in files:
            if not f.endswith(".parquet"):
                continue
            local_path = os.path.join(root, f)
            rel_path   = os.path.relpath(local_path, local_silver)
            s3_key     = f"{s3_silver_prefix}/{rel_path}"
            upload_args.append((local_path, s3_key))

    if not upload_args:
        print(f"⚠️  Aucun fichier Parquet trouvé localement dans {local_silver}")
        return

    def _upload_file(args):
        local_path, s3_key = args
        s3.upload_file(local_path, BUCKET_PUBLIC, s3_key, ExtraArgs={"ACL": "public-read"})

    print(f"📤 Upload de {len(upload_args)} fichier(s) vers S3...")
    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(_upload_file, upload_args))
    print(f"✅ Fichier(s) uploadé(s) vers s3://{BUCKET_PUBLIC}/{s3_silver_prefix}/")

    # ── 2. Télécharger metadata.ducklake existant ─────────────────────
    local_metadata_dir = "ducklake_metadata"
    local_metadata     = os.path.join(local_metadata_dir, "metadata.ducklake")
    local_data_path    = os.path.abspath(os.path.join(local_metadata_dir, "data"))
    os.makedirs(local_data_path, exist_ok=True)

    is_new = False
    try:
        s3.download_file(BUCKET_PUBLIC, "metadata.ducklake", local_metadata)
        print("📥 metadata.ducklake existant récupéré")
    except Exception:
        print("🆕 Nouveau DuckLake — création depuis zéro")
        is_new = True

    # ── 3. Configurer DuckDB + httpfs ─────────────────────────────────
    con = duckdb.connect()
    con.execute("INSTALL httpfs; INSTALL ducklake; LOAD httpfs; LOAD ducklake;")
    con.execute(f"SET s3_endpoint='{OVH_ENDPOINT.replace('https://', '')}'")
    con.execute("SET s3_region='gra'")
    con.execute(f"SET s3_access_key_id='{OVH_ACCESS_KEY}'")
    con.execute(f"SET s3_secret_access_key='{OVH_SECRET_KEY}'")
    con.execute("SET s3_url_style='path'; SET s3_use_ssl=true")

    base_https = f"https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net"
    public_data_path = f"{base_https}/"

    # ── 4. Attacher DuckLake ──────────────────────────────────────────
    if is_new:
        con.execute(f"""
            ATTACH '{local_metadata}' AS ais_lake (
                TYPE ducklake,
                DATA_PATH '{public_data_path}',
                OVERRIDE_DATA_PATH true,
                AUTOMATIC_MIGRATION true
            )
        """)
        con.execute("DETACH ais_lake")
        print(f"💾 DATA_PATH public distant configuré dans le catalogue : {public_data_path}")

    con.execute(f"""
        ATTACH '{local_metadata}' AS ais_lake (
            TYPE ducklake,
            DATA_PATH '{local_data_path}',
            OVERRIDE_DATA_PATH true
        )
    """)

    # ── 5. Créer la table si premier run ──────────────────────────────────────
    if is_new:
        first_url = f"{base_https}/{upload_args[0][1]}"
        con.execute(f"""
            CREATE TABLE ais_lake.messages AS
                SELECT * FROM read_parquet('{first_url}') WITH NO DATA
        """)
        con.execute("ALTER TABLE ais_lake.messages SET PARTITIONED BY (year, month, day);")
        print("🗂️  Table 'messages' partitionnée proprement initialisée dans DuckLake")
        
    # ── 6. Enregistrer le nouveau fichier consolidé (idempotent) ──────────────
    known_paths = {
        row[0]
        for row in con.execute(
            "SELECT path FROM __ducklake_metadata_ais_lake.ducklake_data_file"
        ).fetchall()
    }

    to_register = [
        s3_key for _, s3_key in upload_args
        if f"{base_https}/{s3_key}" not in known_paths
    ]

    print(f"📋 {len(to_register)} nouveau(x) fichier(s) à enregistrer dans le catalogue...")

    for s3_key in to_register:
        public_url = f"{base_https}/{s3_key}"
        con.execute(f"""
            CALL ducklake_add_data_files(
                'ais_lake', 
                'messages', 
                '{public_url}',
                hive_partitioning=True
            )
        """)

    # ── 7. Fermer la connexion — flush vers le .ducklake local ────────
    con.close()
    print("💾 Catalogue DuckLake mis à jour localement")

    # ── 8. Uploader le .ducklake mis à jour vers S3 ───────────────────
    s3.upload_file(
        local_metadata,
        BUCKET_PUBLIC,
        "metadata.ducklake",
        ExtraArgs={"ACL": "public-read"},
    )
    print("📤 metadata.ducklake publié avec succès !")
    print(f"   → Accès public direct : ATTACH 'https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net/metadata.ducklake' AS ais (TYPE ducklake);")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Publication du catalogue DuckLake vers S3 public")
    parser.add_argument(
        "--date", 
        help="Date des fichiers Silver à publier au format YYYY-MM-DD. Si absent, traite la date d'HIER.",
        type=str,
        default=None
    )
    args = parser.parse_args()

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print("❌ Format de date invalide. Utilisez YYYY-MM-DD (ex: 2026-05-26)")
            exit(1)
    else:
        # Par défaut : Hier
        target_date = datetime.now(timezone.utc) - timedelta(days=1)

    publish_ducklake(target_date)