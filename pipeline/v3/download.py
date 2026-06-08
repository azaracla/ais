"""Phase 1: Download raw NDJSON.zst files from S3."""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import BUCKET_RAW


def list_raw_files(s3, target_date):
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


def download_files(s3, keys, dest_dir, max_workers=16):
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
