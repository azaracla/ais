#!/usr/bin/env python3
"""
Bronze → Silver: Nettoyage et conversion en Parquet
- Lit TOUS les fichiers .zst depuis S3 (BUCKET_RAW)
- Nettoie les timestamps invalides (heures > 23)
- Écrit en Parquet partitionné par date dans ./silver/
"""

import polars as pl
from configuration import *
import os
import json
import re
import zstandard as zstd


def clean_timestamp(ts: str) -> str:
    """Corrige les timestamps avec heures > 23"""
    return re.sub(
        r' (\d{1,2}):',
        lambda m: f" {int(m.group(1)) % 24:02d}:",
        ts
    )


def process_all():
    """Traite tous les fichiers depuis S3"""
    import s3fs
    
    fs = s3fs.S3FileSystem(
        client_kwargs={"endpoint_url": OVH_ENDPOINT},
        key=OVH_ACCESS_KEY,
        secret=OVH_SECRET_KEY,
    )
    
    # Lister tous les fichiers .zst
    all_zst = fs.glob(f"{BUCKET_RAW}/**/*.zst")
    
    if not all_zst:
        print("❌ Aucune date trouvée dans S3")
        return
    
    print(f"📥 {len(all_zst)} fichiers .zst trouvés dans S3")
    
    all_records = []
    for zst_file in all_zst:
        print(f"  → {os.path.basename(zst_file)}")
        with fs.open(zst_file, 'rb') as fh:
            dctx = zstd.ZstdDecompressor()
            decompressed = dctx.decompress(fh.read())
            for line in decompressed.decode('utf-8').splitlines():
                if line.strip():
                    all_records.append(json.loads(line))
    
    print(f"📥 {len(all_records)} records lus")
    
    df = pl.DataFrame(all_records)
    
    # Nettoyage
    print("🧹 Nettoyage des timestamps...")
    df = df.with_columns(
        # Corriger time_utc dans metadata
        pl.col("metadata").map_elements(
            lambda x: {**x, "time_utc": clean_timestamp(x.get("time_utc", ""))}
        ).alias("metadata"),
        # Extraire date depuis metadata.time_utc
        pl.col("metadata").map_elements(lambda x: x.get("time_utc", "")[:10]).alias("__date")
    )
    
    # Écrire en Parquet partitionné par date
    output_path = "./silver/"
    os.makedirs(output_path, exist_ok=True)
    print(f"📤 Écriture vers {output_path}...")
    
    # Conversion en JSON string pour éviter les problèmes de struct
    df = df.with_columns(
        pl.col("metadata").map_elements(json.dumps).alias("metadata"),
        pl.col("message").map_elements(json.dumps).alias("message")
    )
    
    df.write_parquet(output_path, partition_by=["__date"])
    
    print(f"✅ Terminé ! {len(all_records)} records traités.")


if __name__ == "__main__":
    process_all()
