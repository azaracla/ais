"""Fetch and parse UN/LOCODE port database.

Downloads the UN/LOCODE code-list CSV and produces data/ports.parquet
with clean port entries suitable for geofence-based arrival detection.
"""

import urllib.request
import duckdb

from utils import DATA_DIR

UNLOCODE_URL = (
    "https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv"
)
OUTPUT = DATA_DIR / "ports.parquet"


def parse_coord(coord: str) -> float | None:
    """Parse UN/LOCODE coordinate format like '4230N 00131E' to decimal degrees.

    Returns None if the coordinate is empty or unparseable.
    """
    if not coord or not coord.strip():
        return None
    parts = coord.strip().split()
    if len(parts) != 2:
        return None

    # Latitude: "4230N" or "4230S" — 2+2 digits + hemisphere
    lat_str = parts[0].strip()
    if len(lat_str) < 5:
        return None
    try:
        lat_deg = int(lat_str[:2])
        lat_min = int(lat_str[2:4])
        lat_hem = lat_str[4]
    except ValueError:
        return None
    lat = lat_deg + lat_min / 60.0
    if lat_hem == "S":
        lat = -lat
    elif lat_hem != "N":
        return None

    # Longitude: "00131E" or "00131W" — 3+2 digits + hemisphere
    lon_str = parts[1].strip()
    if len(lon_str) < 6:
        return None
    try:
        lon_deg = int(lon_str[:3])
        lon_min = int(lon_str[3:5])
        lon_hem = lon_str[5]
    except ValueError:
        return None
    lon = lon_deg + lon_min / 60.0
    if lon_hem == "W":
        lon = -lon
    elif lon_hem != "E":
        return None

    return lat, lon


def fetch():
    """Download and parse UN/LOCODE CSV, produce ports.parquet."""
    print(f"Downloading {UNLOCODE_URL} ...")
    raw_csv = OUTPUT.with_suffix(".csv")
    urllib.request.urlretrieve(UNLOCODE_URL, raw_csv)

    print("Parsing into DuckDB ...")
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE raw AS
        SELECT * FROM read_csv(?, header=true, auto_detect=true, all_varchar=true)
    """, [str(raw_csv)])

    # Clean and filter: keep only entries with coordinates, parse lat/lon.
    # LOCODE = Country + Location (e.g. NL + RTM = NLRTM).
    con.execute("""
        CREATE TABLE ports AS
        SELECT
            "Country" || "Location" AS lo_code,
            "Country" AS country,
            "Name" AS name,
            "NameWoDiacritics" AS name_ascii,
            CASE WHEN "Function" LIKE '%1%' THEN 1 ELSE 0 END AS is_port,
            "Coordinates" AS coord_raw,
            "Subdivision" AS subdivision,
            "Status" AS status
        FROM raw
        WHERE "Coordinates" IS NOT NULL
          AND TRIM("Coordinates") != ''
          AND "Location" IS NOT NULL
          AND TRIM("Location") != ''
    """)

    # Export — coord parsing done in Python below, or add as computed columns
    # DuckDB doesn't easily parse "4230N 00131E" format natively,
    # so we parse in Python and join back.
    rows = con.execute("SELECT rowid, coord_raw FROM ports").fetchall()

    lats, lons = {}, {}
    for rowid, coord_raw in rows:
        result = parse_coord(coord_raw)
        if result:
            lats[rowid] = result[0]
            lons[rowid] = result[1]

    # Add parsed lat/lon columns
    con.execute("ALTER TABLE ports ADD COLUMN lat DOUBLE DEFAULT NULL")
    con.execute("ALTER TABLE ports ADD COLUMN lon DOUBLE DEFAULT NULL")

    for rowid in lats:
        con.execute(
            f"UPDATE ports SET lat = {lats[rowid]}, lon = {lons[rowid]} WHERE rowid = {rowid}"
        )

    # Remove rows where parsing failed
    con.execute("DELETE FROM ports WHERE lat IS NULL")

    # Remove duplicates: keep each lo_code once, preferring is_port=1
    con.execute("""
        DELETE FROM ports
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM ports
            GROUP BY lo_code
        )
    """)

    count = con.execute("SELECT count(*) FROM ports").fetchone()[0]
    print(f"Exporting {count} ports to {OUTPUT} ...")

    con.execute(f"COPY (SELECT * FROM ports ORDER BY country, lo_code) TO '{OUTPUT}' (FORMAT PARQUET, COMPRESSION ZSTD)")

    # Cleanup
    raw_csv.unlink(missing_ok=True)
    con.close()
    print(f"Done. {OUTPUT} ({count} ports)")


if __name__ == "__main__":
    fetch()
