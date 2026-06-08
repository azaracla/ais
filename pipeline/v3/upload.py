"""Phase 4: Upload generated Parquet files to S3."""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import BUCKET_PUBLIC, S3_DATA_PREFIX, BASE_HTTPS


def upload_output_files(s3, output_dir_path, max_workers=32):
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
                print(f"   📤 {i + 1}/{len(all_uploads)}...")

    print(f"   ✅ {uploaded}/{len(all_uploads)} fichiers uploadés")

    return [
        (local_path, f"{BASE_HTTPS}/{s3_key}")
        for local_path, s3_key in all_uploads
    ]
