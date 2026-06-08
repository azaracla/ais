"""Shared paths, constants, and helpers for the v3 pipeline."""

import os
import re
import sys
import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from configuration import *  # noqa: E402, F403 — loads OVH_*, BUCKET_* from .env

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SQL_DIR = os.path.join(SCRIPT_DIR, 'sql')
WORK_BASE_DIR = os.path.join(SCRIPT_DIR, 'work')
OUTPUT_BASE_DIR = os.path.join(SCRIPT_DIR, 'output')
CATALOG_DIR = os.path.join(SCRIPT_DIR, 'catalog')
CATALOG_FILE = os.path.join(CATALOG_DIR, 'ais.ducklake')

S3_CATALOG_KEY = "v3/ais.ducklake"
S3_DATA_PREFIX = "v3/ais.ducklake.files"
PORTS_PARQUET = os.path.join(SCRIPT_DIR, '..', '..', 'ml', 'data', 'ports.parquet')
BASE_HTTPS = f"https://{BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net"


def work_dir(date):
    from datetime import datetime  # noqa: F811
    return os.path.join(WORK_BASE_DIR, date.strftime('%Y-%m-%d'))


def output_dir(date):
    return os.path.join(OUTPUT_BASE_DIR, date.strftime('%Y-%m-%d'))


def s3_client():
    return boto3.client(
        's3',
        endpoint_url=OVH_ENDPOINT,
        aws_access_key_id=OVH_ACCESS_KEY,
        aws_secret_access_key=OVH_SECRET_KEY,
        region_name=OVH_REGION,
        config=boto3.session.Config(s3={'addressing_style': 'path'}),
    )


def load_sql(name):
    with open(os.path.join(SQL_DIR, name)) as f:
        return f.read()


def run_sql(con, sql, params):
    for key, val in params.items():
        if isinstance(val, str):
            quoted = f"'{val}'"
        elif isinstance(val, bool):
            quoted = 'true' if val else 'false'
        elif val is None:
            quoted = 'NULL'
        else:
            quoted = str(val)
        sql = sql.replace(f':{key}', quoted)
    con.execute(sql)


_JUNK_PATTERNS = [
    re.compile(r"^[?@]{2,}$"),
    re.compile(r"^-{3,}$"),
    re.compile(r"^0+$"),
    re.compile(r"^[Xx]{2,}$"),
    re.compile(r"^\+?$"),
]


def clean_destination(raw):
    if raw is None:
        return None
    s = raw.strip().upper()
    if not s:
        return None
    s = re.sub(r"[^\x20-\x7E]", "", s)
    if not s:
        return None
    for pat in _JUNK_PATTERNS:
        if pat.match(s):
            return None
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 2:
        return None
    return s
