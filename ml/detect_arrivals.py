"""Detect vessel arrivals from AIS positions — two-pass approach.

Pass 1: Export ETA declarations and relevant positions from remote catalog to local Parquet.
Pass 2: Process locally — match destinations to ports, detect arrivals via speed/geofence.
"""

import duckdb
import math
from utils import (
    DATA_DIR,
    connect_catalog,
    clean_destination,
    is_lo_code,
    haversine_km,
)

OUTPUT = DATA_DIR / "arrivals.parquet"
PORTS_PARQUET = DATA_DIR / "ports.parquet"
ETA_LOCAL = DATA_DIR / "eta_declarations.parquet"
POS_LOCAL = DATA_DIR / "positions_filtered.parquet"

PORT_RADIUS_KM = 10.0
STOP_SOG_KNOTS = 0.5
STOP_MIN_MINUTES = 30


# ── Pass 1: Export from remote catalog ─────────────────────────────────────────

def export_data(con, days: list[tuple[int, int, int]] | None = None):
    """Export ETA declarations and relevant positions to local Parquet files.

    Args:
        days: list of (year, month, day) tuples. If None, exports all available data.
    """
    if days is None:
        day_filter = ""
        pos_day_filter = ""
    else:
        day_clauses = [f"(year={y} AND month={m} AND day={d})" for y, m, d in days]
        day_filter = "AND (" + " OR ".join(day_clauses) + ")"
        pos_day_filter = day_filter

    print(f"Exporting ETA declarations ({'all' if days is None else f'{len(days)} days'}) ...")
    con.execute(f"""
        COPY (
            SELECT mmsi, destination, eta, ts AS static_ts
            FROM ais.messages
            WHERE eta IS NOT NULL
              AND destination IS NOT NULL
              AND destination != ''
              AND message_type = 'ShipStaticData'
              {day_filter}
        ) TO '{ETA_LOCAL}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{ETA_LOCAL}')").fetchone()[0]
    print(f"  {n} ETA declarations")

    print("Exporting positions for relevant MMSIs ...")
    con.execute(f"""
        CREATE TEMP TABLE target_mmsis AS
        SELECT DISTINCT mmsi FROM read_parquet('{ETA_LOCAL}')
    """)
    n_mmsi = con.execute("SELECT count(*) FROM target_mmsis").fetchone()[0]
    print(f"  {n_mmsi} distinct MMSIs")

    con.execute(f"""
        COPY (
            SELECT vp.mmsi, vp.ts, vp.lat, vp.lon, vp.sog, vp.cog, vp.true_heading,
                   vp.navigational_status, vp.rate_of_turn,
                   vp.year, vp.month, vp.day
            FROM ais.vessels_positions vp
            SEMI JOIN target_mmsis t ON vp.mmsi = t.mmsi
            WHERE 1=1 {pos_day_filter}
            ORDER BY vp.mmsi, vp.ts
        ) TO '{POS_LOCAL}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n_pos = con.execute(f"SELECT count(*) FROM read_parquet('{POS_LOCAL}')").fetchone()[0]
    print(f"  {n_pos} positions")


# ── Pass 2: Local processing ───────────────────────────────────────────────────

def build_matched(con):
    """Match destinations to ports using in-memory dict lookups (fast)."""
    print("\nMatching destinations to ports ...")

    # Dedup ETA
    con.execute(f"""
        CREATE TABLE eta_dedup AS
        SELECT mmsi, destination, eta, MAX(static_ts) AS static_ts
        FROM read_parquet('{ETA_LOCAL}')
        GROUP BY mmsi, destination, eta
    """)
    n = con.execute("SELECT count(*) FROM eta_dedup").fetchone()[0]
    print(f"  {n} unique declarations")

    # Load all ports into memory
    ports_rows = con.execute(f"""
        SELECT lo_code, name, name_ascii, lat, lon, is_port
        FROM read_parquet('{PORTS_PARQUET}')
    """).fetchall()

    # Build lookup dicts
    lo_lookup = {}       # lo_code → (name, lat, lon)  for is_port=1
    name_lookup = {}     # name → (lo_code, name, lat, lon) for is_port=1
    name_any = {}        # name → (lo_code, name, lat, lon) for any status

    for lo, name, name_ascii, lat, lon, is_port in ports_rows:
        key_lo = lo.upper() if lo else ""
        key_name = name.upper().strip() if name else ""
        key_ascii = name_ascii.upper().strip() if name_ascii else ""

        if is_port:
            if key_lo:
                lo_lookup[key_lo] = (name, lat, lon)
            if key_name:
                name_lookup[key_name] = (key_lo, name, lat, lon)
            if key_ascii and key_ascii != key_name:
                name_lookup[key_ascii] = (key_lo, name, lat, lon)
        else:
            if key_name:
                name_any[key_name] = (key_lo, name, lat, lon)
            if key_ascii and key_ascii != key_name:
                name_any[key_ascii] = (key_lo, name, lat, lon)

    print(f"  Dicts: {len(lo_lookup)} lo_codes, {len(name_lookup)} names (port), {len(name_any)} names (any)")

    # Match each declaration
    rows = con.execute(
        "SELECT mmsi, destination, eta, static_ts FROM eta_dedup"
    ).fetchall()

    matched = []
    lo_hits = name_hits = 0

    for mmsi, dest_raw, eta, static_ts in rows:
        dest_clean = clean_destination(dest_raw)
        if dest_clean is None:
            continue

        port_lo = port_name = port_lat = port_lon = ""
        method = "none"

        # 1. LOCODE exact match
        if dest_clean in lo_lookup:
            port_name, port_lat, port_lon = lo_lookup[dest_clean]
            port_lo = dest_clean
            method = "lo_code"
            lo_hits += 1

        # 2. Name match (is_port=1 preferred)
        elif dest_clean in name_lookup:
            port_lo, port_name, port_lat, port_lon = name_lookup[dest_clean]
            method = "name"
            name_hits += 1

        # 3. Name match (any status)
        elif dest_clean in name_any:
            port_lo, port_name, port_lat, port_lon = name_any[dest_clean]
            method = "name_any"
            name_hits += 1

        matched.append((
            mmsi, dest_raw, dest_clean, eta, static_ts,
            port_lo, port_name, port_lat if port_lat else 0.0, port_lon if port_lon else 0.0,
            method,
        ))

    print(f"  Matched: {len(matched)} total (lo_code: {lo_hits}, name: {name_hits})")

    # Dump matched into DuckDB
    con.execute("""
        CREATE TABLE matched (
            mmsi BIGINT, destination_raw VARCHAR, destination_clean VARCHAR,
            eta TIMESTAMP WITH TIME ZONE, static_ts TIMESTAMP WITH TIME ZONE,
            port_lo_code VARCHAR, port_name VARCHAR, port_lat DOUBLE, port_lon DOUBLE,
            match_method VARCHAR
        )
    """)
    for i in range(0, len(matched), 5000):
        batch = matched[i : i + 5000]
        ph = ", ".join(["(?,?,?,?,?,?,?,?,?,?)"] * len(batch))
        flat = [x for row in batch for x in row]
        con.execute(f"INSERT INTO matched VALUES {ph}", flat)


def detect_speed_stops(con):
    """Speed-based arrival detection: SOG < threshold for >= N minutes."""
    print("\nSpeed-based stop detection ...")

    # Join positions with matched ETA declarations, filter by time window
    con.execute(f"""
        CREATE TABLE pos AS
        SELECT p.*, m.eta, m.static_ts, m.destination_clean, m.destination_raw,
               m.port_lo_code, m.port_lat, m.port_lon
        FROM read_parquet('{POS_LOCAL}') p
        JOIN matched m ON p.mmsi = m.mmsi
        WHERE p.ts >= m.static_ts - INTERVAL 1 DAY
          AND p.ts <= COALESCE(m.eta, m.static_ts) + INTERVAL '7' DAY
    """)

    n_pos = con.execute("SELECT count(*) FROM pos").fetchone()[0]
    print(f"  {n_pos} positions in ETA window")

    if n_pos == 0:
        con.execute("""
            CREATE TABLE arrivals_speed (
                mmsi BIGINT, destination_raw VARCHAR, destination_clean VARCHAR,
                port_lo_code VARCHAR, port_lat DOUBLE, port_lon DOUBLE,
                arrival_ts TIMESTAMP WITH TIME ZONE, arrival_lat DOUBLE, arrival_lon DOUBLE,
                eta TIMESTAMP WITH TIME ZONE, static_ts TIMESTAMP WITH TIME ZONE,
                detection_method VARCHAR
            )
        """)
        return

    # Tag stopped positions and group consecutive stops
    con.execute(f"""
        CREATE TABLE stops_grouped AS
        SELECT *,
            SUM(CASE WHEN sog >= {STOP_SOG_KNOTS} THEN 1 ELSE 0 END)
                OVER (PARTITION BY mmsi ORDER BY ts
                      ROWS UNBOUNDED PRECEDING) AS stop_grp
        FROM pos
        WHERE sog IS NOT NULL
    """)

    # Aggregate stops lasting >= STOP_MIN_MINUTES
    con.execute(f"""
        CREATE TABLE stop_events AS
        SELECT
            mmsi, stop_grp,
            MIN(ts) AS stop_start, MAX(ts) AS stop_end,
            FIRST(lat ORDER BY ts) AS stop_lat,
            FIRST(lon ORDER BY ts) AS stop_lon,
            FIRST(eta ORDER BY ts) AS eta,
            FIRST(static_ts ORDER BY ts) AS static_ts,
            FIRST(destination_clean ORDER BY ts) AS dest_clean,
            FIRST(destination_raw ORDER BY ts) AS dest_raw,
            FIRST(port_lo_code ORDER BY ts) AS port_lo,
            FIRST(port_lat ORDER BY ts) AS port_lat,
            FIRST(port_lon ORDER BY ts) AS port_lon,
            COUNT(*) AS n_pos,
            DATEDIFF('minute', MIN(ts), MAX(ts)) AS dur_min
        FROM stops_grouped
        WHERE sog < {STOP_SOG_KNOTS}
        GROUP BY mmsi, stop_grp
        HAVING DATEDIFF('minute', MIN(ts), MAX(ts)) >= {STOP_MIN_MINUTES}
    """)

    n_stops = con.execute("SELECT count(*) FROM stop_events").fetchone()[0]
    print(f"  Stops >= {STOP_MIN_MINUTES} min: {n_stops}")

    # First stop after static_ts for each (mmsi, dest, eta)
    con.execute("""
        CREATE TABLE arrivals_speed AS
        SELECT DISTINCT ON (mmsi, dest_clean, eta)
            mmsi, dest_raw AS destination_raw, dest_clean AS destination_clean,
            port_lo AS port_lo_code, port_lat, port_lon,
            stop_start AS arrival_ts, stop_lat AS arrival_lat, stop_lon AS arrival_lon,
            eta, static_ts,
            'speed' AS detection_method
        FROM stop_events
        WHERE stop_start >= static_ts - INTERVAL 1 DAY
        ORDER BY mmsi, dest_clean, eta, stop_start ASC
    """)

    c = con.execute("SELECT count(*) FROM arrivals_speed").fetchone()[0]
    print(f"  Speed arrivals: {c}")


def detect_geofence(con):
    """Geofence detection: vessel enters port radius + slows down."""
    print("\nGeofence detection ...")

    ports = con.execute("""
        SELECT DISTINCT port_lo_code, port_lat, port_lon
        FROM matched WHERE port_lo_code != ''
    """).fetchall()

    con.execute("""
        CREATE TABLE arrivals_geofence (
            mmsi BIGINT, destination_raw VARCHAR, destination_clean VARCHAR,
            port_lo_code VARCHAR, port_lat DOUBLE, port_lon DOUBLE,
            arrival_ts TIMESTAMP WITH TIME ZONE, arrival_lat DOUBLE, arrival_lon DOUBLE,
            eta TIMESTAMP WITH TIME ZONE, static_ts TIMESTAMP WITH TIME ZONE,
            detection_method VARCHAR
        )
    """)

    if not ports:
        print("  No matched ports, skipping.")
        return

    print(f"  Checking {len(ports)} ports ...")
    all_arrivals = []

    for port_lo, port_lat, port_lon in ports:
        dlat = 0.2  # ~22 km
        dlon = 0.2 / math.cos(math.radians(port_lat)) if abs(port_lat) < 89 else 0.5

        rows = con.execute(f"""
            SELECT p.mmsi, p.ts, p.lat, p.lon, p.sog,
                   m.eta, m.static_ts, m.destination_clean, m.destination_raw
            FROM read_parquet('{POS_LOCAL}') p
            JOIN matched m ON p.mmsi = m.mmsi AND m.port_lo_code = '{port_lo}'
            WHERE p.lat BETWEEN {port_lat - dlat} AND {port_lat + dlat}
              AND p.lon BETWEEN {port_lon - dlon} AND {port_lon + dlon}
              AND p.ts >= m.static_ts - INTERVAL 1 DAY
              AND p.ts <= COALESCE(m.eta, m.static_ts) + INTERVAL '7' DAY
              AND p.sog <= 5.0
            ORDER BY p.mmsi, p.ts
        """).fetchall()

        seen = {}
        for row in rows:
            mmsi, ts, lat, lon, sog, eta, sts, dc, dr = row
            if mmsi in seen:
                continue
            d = haversine_km(lat, lon, port_lat, port_lon)
            if d <= PORT_RADIUS_KM and sog is not None and sog <= 1.0:
                seen[mmsi] = row

        for row in seen.values():
            mmsi, ts, lat, lon, sog, eta, sts, dc, dr = row
            all_arrivals.append((
                mmsi, dr, dc, port_lo, port_lat, port_lon,
                ts, lat, lon, eta, sts, "geofence",
            ))

    print(f"  Geofence arrivals: {len(all_arrivals)}")

    if all_arrivals:
        for i in range(0, len(all_arrivals), 5000):
            batch = all_arrivals[i : i + 5000]
            ph = ", ".join(["(?,?,?,?,?,?,?,?,?,?,?,?)"] * len(batch))
            flat = [x for row in batch for x in row]
            con.execute(f"INSERT INTO arrivals_geofence VALUES {ph}", flat)


def merge_and_export(con):
    """Merge geofence (priority) + speed (fallback), export to Parquet."""
    print("\nMerging ...")

    geof = con.execute("SELECT count(*) FROM arrivals_geofence").fetchone()[0]
    spd = con.execute("SELECT count(*) FROM arrivals_speed").fetchone()[0]
    print(f"  Geofence: {geof}, Speed: {spd}")

    con.execute("""
        CREATE TABLE arrivals AS
        SELECT * FROM arrivals_geofence
        UNION ALL
        SELECT a.* FROM arrivals_speed a
        WHERE NOT EXISTS (
            SELECT 1 FROM arrivals_geofence g
            WHERE g.mmsi = a.mmsi
              AND g.destination_clean = a.destination_clean
              AND g.eta = a.eta
        )
    """)

    total = con.execute("SELECT count(*) FROM arrivals").fetchone()[0]
    print(f"  Total: {total}")

    if total == 0:
        print("ERROR: No arrivals detected!")
        return 0

    con.execute(f"""
        COPY (SELECT * FROM arrivals ORDER BY mmsi, arrival_ts)
        TO '{OUTPUT}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    print(f"Exported to {OUTPUT}")

    # Stats
    print("\nBy method:")
    for row in con.execute(
        "SELECT detection_method, count(*) FROM arrivals GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {row[0]}: {row[1]}")

    print("\nSample:")
    for row in con.execute("""
        SELECT mmsi, destination_clean, port_lo_code, arrival_ts, eta,
               detection_method,
               DATEDIFF('hour', eta, arrival_ts) AS eta_err_h
        FROM arrivals ORDER BY arrival_ts DESC LIMIT 10
    """).fetchall():
        print(f"  mmsi={row[0]} → {row[1]} ({row[2]}) "
              f"arrived={row[3]} eta={row[4]} err={row[6]}h [{row[5]}]")

    return total


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Arrival Detection")
    print("=" * 60)

    # ── Pass 1: Export ──
    print("\n── Pass 1: Export from catalog ──")
    remote = connect_catalog(read_only=True)

    # All 12 days available (May 26 – Jun 6)
    days = [
        (2026, 5, 26), (2026, 5, 27), (2026, 5, 28), (2026, 5, 29),
        (2026, 5, 30), (2026, 5, 31),
        (2026, 6, 1), (2026, 6, 2), (2026, 6, 3),
        (2026, 6, 4), (2026, 6, 5), (2026, 6, 6),
    ]
    export_data(remote, days=days)
    remote.close()

    # ── Pass 2: Process locally ──
    print("\n── Pass 2: Local processing ──")
    local = duckdb.connect()
    build_matched(local)
    detect_speed_stops(local)
    detect_geofence(local)
    total = merge_and_export(local)
    local.close()

    print(f"\n✓ Done. {total} arrivals → {OUTPUT}")


if __name__ == "__main__":
    main()
