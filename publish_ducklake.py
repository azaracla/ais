#!/usr/bin/env python3
import duckdb
import boto3
import os
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from configuration import *


def publish_ducklake():
    today = datetime.now(timezone.utc)

    # ── 1. Upload des Parquet locaux vers S3 ─────────────────────────
    local_silver = f"silver/year={today.year}/month={today.month:02d}/day={today.day:02d}"
    s3_silver_prefix = f"main/messages/year={today.year}/month={today.month:02d}/day={today.day:02d}"

    # Config S3 optimisée pour upload rapide
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
        region_name=OVH_REGION,  # Région par défaut pour éviter les erreurs de signature
        config=config,
    )

    # Préparer la liste des fichiers à uploader
    upload_args = []
    for root, _, files in os.walk(local_silver):
        for f in files:
            local_path = os.path.join(root, f)
            rel_path = os.path.relpath(local_path, local_silver)
            s3_key = f"{s3_silver_prefix}/{rel_path}"
            upload_args.append((local_path, s3_key))

    # Upload parallélisé avec ThreadPoolExecutor
    def _upload_file(args):
        local_path, s3_key = args
        s3.upload_file(local_path, BUCKET_PUBLIC, s3_key, ExtraArgs={'ACL': 'public-read'})

    print(f"📤 Upload de {len(upload_args)} fichiers vers S3 (32 threads)...")
    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(_upload_file, upload_args))
    print(f"✅ {len(upload_args)} fichiers uploadés vers s3://{BUCKET_PUBLIC}/{s3_silver_prefix}/")

    # ── 2. Télécharger metadata.ducklake existant ─────────────────────
    local_metadata_dir = "ducklake_metadata"
    local_metadata = os.path.join(local_metadata_dir, "metadata.ducklake")
    os.makedirs(local_metadata_dir, exist_ok=True)

    try:
        s3.download_file(BUCKET_PUBLIC, "metadata.ducklake", local_metadata)
        print("📥 metadata.ducklake existant récupéré")
    except Exception:
        print("🆕 Nouveau DuckLake (aucune version existante)")

    # ── 3. Configurer DuckDB + S3 ─────────────────────────────────────
    con = duckdb.connect()
    con.execute("INSTALL httpfs; INSTALL ducklake; LOAD httpfs; LOAD ducklake;")
    con.execute(f"SET s3_endpoint='{OVH_ENDPOINT.replace('https://', '')}'")
    con.execute("SET s3_region='gra'")
    con.execute(f"SET s3_access_key_id='{OVH_ACCESS_KEY}'")
    con.execute(f"SET s3_secret_access_key='{OVH_SECRET_KEY}'")
    con.execute("SET s3_url_style='path'; SET s3_use_ssl=true")

    # ── 4. Attacher DuckLake (DATA_PATH = HTTPS pour accès public) ────────────
    data_uri = f"https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net/silver/"
    con.execute(f"ATTACH '{local_metadata}' AS ais_lake (TYPE ducklake, DATA_PATH '{data_uri}')")

    # ── 5. Exporter + uploader metadata (DuckLake détecte auto les fichiers) ──
    print("💾 Export du metadata...")
    con.execute(f"EXPORT DATABASE ais_lake TO '{local_metadata_dir}'")
    s3.upload_file(
        os.path.join(local_metadata_dir, "metadata.ducklake"),
        BUCKET_PUBLIC,
        "metadata.ducklake",
        ExtraArgs={'ACL': 'public-read'}
    )
    print("📤 metadata.ducklake uploadé (remplace l'ancien)")

    con.close()
    print("✅ DuckLake prêt pour lecture publique !")


if __name__ == "__main__":
    publish_ducklake()
