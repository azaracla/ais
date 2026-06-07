"""Shared utilities for ML ETA prediction.

Haversine, bearing, destination cleaning, DuckDB catalog connection.
"""

import re
import duckdb
from math import asin, atan2, cos, radians, sin, sqrt
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
ML_DIR = Path(__file__).parent.resolve()
DATA_DIR = ML_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


# ── DuckDB + DuckLake catalog ───────────────────────────────────────────────────

def connect_catalog(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """Connect to the remote DuckLake catalog (public HTTPS, read-only)."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs")
    con.execute("INSTALL ducklake; LOAD ducklake")
    con.execute("SET enable_http_metadata_cache=false")

    catalog_url = "https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake"
    data_path = "https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake.files/"

    con.execute(f"""
        ATTACH '{catalog_url}' AS ais (
            TYPE ducklake,
            DATA_PATH '{data_path}',
            OVERRIDE_DATA_PATH true
        )
    """)
    return con


# ── Geospatial ──────────────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in kilometers between two (lat, lon) pairs."""
    r = 6371.0  # Earth radius in km
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing (degrees) from point 1 to point 2."""
    dlon = radians(lon2 - lon1)
    y = sin(dlon) * cos(radians(lat2))
    x = cos(radians(lat1)) * sin(radians(lat2)) - sin(radians(lat1)) * cos(radians(lat2)) * cos(dlon)
    return (atan2(y, x) * 180.0 / 3.141592653589793 + 360) % 360


# ── Destination cleaning ────────────────────────────────────────────────────────

# Common AIS destination junk patterns
_JUNK_PATTERNS = [
    re.compile(r"^[?@]{2,}$"),          # "??????", "@@@@"
    re.compile(r"^-{3,}$"),              # "------"
    re.compile(r"^0+$"),                 # "00000"
    re.compile(r"^[Xx]{2,}$"),           # "XXXX"
    re.compile(r"^\+?$"),                # "+"
]


def clean_destination(raw: str | None) -> str | None:
    """Normalize a destination string from AIS.

    Returns cleaned uppercase string, or None if junk/unusable.
    """
    if raw is None:
        return None
    s = raw.strip().upper()
    if not s:
        return None
    # Remove non-printable characters
    s = re.sub(r"[^\x20-\x7E]", "", s)
    if not s:
        return None
    # Check junk patterns
    for pat in _JUNK_PATTERNS:
        if pat.match(s):
            return None
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 2:
        return None
    return s


def is_lo_code(s: str) -> bool:
    """Check if string looks like a UN/LOCODE (e.g. 'NLRTM', 'DEHAM')."""
    return bool(re.match(r"^[A-Z]{2}[A-Z0-9]{3}$", s))
