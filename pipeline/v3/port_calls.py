"""Port call detection: match AIS destinations → UN/LOCODE ports, detect arrivals and departures."""

import os
from config import clean_destination


def detect_port_calls(con, silver_file, gold_dir, ports_path,
                       existing_path, has_existing):
    """
    Detect port calls from today's silver, merge with existing.
    Detects arrivals (geofence + speed-stop) and departures.
    Returns (output_path, count).
    """
    PORT_RADIUS_KM = 10.0
    STOP_SOG_KNOTS = 0.5
    STOP_MIN_MINUTES = 30

    pc_dir = os.path.join(gold_dir, 'port_calls')
    os.makedirs(pc_dir, exist_ok=True)
    port_calls_file = os.path.join(pc_dir, 'port_calls.parquet')

    con.execute("""
        CREATE MACRO IF NOT EXISTS haversine_km(lat1, lon1, lat2, lon2) AS (
            6371 * 2 * asin(sqrt(
                pow(sin(radians((lat2 - lat1) / 2.0)), 2) +
                cos(radians(lat1)) * cos(radians(lat2)) *
                pow(sin(radians((lon2 - lon1) / 2.0)), 2)
            ))
        )
    """)

    ports_rows = con.execute(f"""
        SELECT lo_code, name, name_ascii, lat, lon, is_port
        FROM read_parquet('{ports_path}')
    """).fetchall()

    lo_lookup = {}
    name_lookup = {}
    name_any = {}
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

    declarations = con.execute(f"""
        SELECT DISTINCT ON (mmsi, destination)
            mmsi, destination, eta, ts AS static_ts
        FROM read_parquet('{silver_file}')
        WHERE message_type = 'ShipStaticData'
          AND destination IS NOT NULL
          AND destination != ''
          AND eta IS NOT NULL
        ORDER BY mmsi, destination, ts DESC
    """).fetchall()

    matched = []
    for mmsi, dest_raw, eta, static_ts in declarations:
        dest_clean = clean_destination(dest_raw)
        if dest_clean is None:
            continue
        if dest_clean in lo_lookup:
            port_name, port_lat, port_lon = lo_lookup[dest_clean]
            matched.append((mmsi, dest_raw, dest_clean, eta, static_ts,
                            dest_clean, port_name, port_lat, port_lon))
        elif dest_clean in name_lookup:
            port_lo, port_name, port_lat, port_lon = name_lookup[dest_clean]
            matched.append((mmsi, dest_raw, dest_clean, eta, static_ts,
                            port_lo, port_name, port_lat, port_lon))
        elif dest_clean in name_any:
            port_lo, port_name, port_lat, port_lon = name_any[dest_clean]
            matched.append((mmsi, dest_raw, dest_clean, eta, static_ts,
                            port_lo, port_name, port_lat, port_lon))

    print(f"   🏗️  Port calls: {len(matched)} déclarations matchées")

    n_pos = 0
    if matched:
        con.execute("""
            CREATE TEMP TABLE matched_decl (
                mmsi BIGINT, dest_raw VARCHAR, dest_clean VARCHAR,
                eta TIMESTAMPTZ, static_ts TIMESTAMPTZ,
                port_lo VARCHAR, port_name VARCHAR, port_lat DOUBLE, port_lon DOUBLE
            )
        """)
        for i in range(0, len(matched), 5000):
            batch = matched[i:i + 5000]
            ph = ", ".join(["(?,?,?,?,?,?,?,?,?)"] * len(batch))
            flat = [x for row in batch for x in row]
            con.execute(f"INSERT INTO matched_decl VALUES {ph}", flat)

        con.execute(f"""
            CREATE TEMP TABLE pos_eta AS
            SELECT p.mmsi, p.ts, p.lat, p.lon, p.sog, p.cog, p.navigational_status,
                   m.port_lo, m.port_name, m.port_lat, m.port_lon,
                   m.eta, m.static_ts, m.dest_clean
            FROM (
                SELECT mmsi, ts, lat, lon, sog, cog, navigational_status
                FROM read_parquet('{silver_file}')
                WHERE message_type IN ('PositionReport', 'ExtendedClassBPositionReport',
                                       'StandardClassBPositionReport')
                  AND lat IS NOT NULL AND lon IS NOT NULL
            ) p
            JOIN matched_decl m ON p.mmsi = m.mmsi
            WHERE p.ts >= m.static_ts - INTERVAL 1 DAY
              AND p.ts <= COALESCE(m.eta, m.static_ts) + INTERVAL 7 DAY
        """)

        n_pos = con.execute("SELECT COUNT(*) FROM pos_eta").fetchone()[0]
        print(f"   📍 {n_pos} positions dans la fenêtre ETA")

        con.execute(f"""
            CREATE TEMP TABLE arrivals_geo AS
            SELECT DISTINCT ON (mmsi, port_lo)
                mmsi, dest_clean AS destination_clean,
                port_lo AS port_lo_code, port_name, port_lat, port_lon,
                ts AS arrival_ts, lat AS arrival_lat, lon AS arrival_lon,
                eta, static_ts,
                'geofence' AS detection_method
            FROM pos_eta
            WHERE sog IS NOT NULL AND sog <= 1.0
              AND haversine_km(lat, lon, port_lat, port_lon) <= {PORT_RADIUS_KM}
            ORDER BY mmsi, port_lo, ts ASC
        """)
        n_geo = con.execute("SELECT COUNT(*) FROM arrivals_geo").fetchone()[0]
        print(f"   🎯 Arrivées geofence: {n_geo}")

        if n_pos > 0:
            con.execute(f"""
                CREATE TEMP TABLE stops_grouped AS
                SELECT *,
                    SUM(CASE WHEN sog >= {STOP_SOG_KNOTS} THEN 1 ELSE 0 END)
                        OVER (PARTITION BY mmsi ORDER BY ts
                              ROWS UNBOUNDED PRECEDING) AS stop_grp
                FROM pos_eta
                WHERE sog IS NOT NULL
            """)
            con.execute(f"""
                CREATE TEMP TABLE stop_events AS
                SELECT
                    mmsi, stop_grp,
                    MIN(ts) AS stop_start, MAX(ts) AS stop_end,
                    FIRST(lat ORDER BY ts) AS stop_lat,
                    FIRST(lon ORDER BY ts) AS stop_lon,
                    FIRST(eta ORDER BY ts) AS eta,
                    FIRST(static_ts ORDER BY ts) AS static_ts,
                    FIRST(dest_clean ORDER BY ts) AS dest_clean,
                    FIRST(port_lo ORDER BY ts) AS port_lo,
                    FIRST(port_name ORDER BY ts) AS port_name,
                    FIRST(port_lat ORDER BY ts) AS port_lat,
                    FIRST(port_lon ORDER BY ts) AS port_lon,
                    DATEDIFF('minute', MIN(ts), MAX(ts)) AS dur_min
                FROM stops_grouped
                WHERE sog < {STOP_SOG_KNOTS}
                GROUP BY mmsi, stop_grp
                HAVING DATEDIFF('minute', MIN(ts), MAX(ts)) >= {STOP_MIN_MINUTES}
            """)
            con.execute("""
                CREATE TEMP TABLE arrivals_speed AS
                SELECT DISTINCT ON (mmsi, dest_clean, eta)
                    mmsi, dest_clean AS destination_clean,
                    port_lo AS port_lo_code, port_name, port_lat, port_lon,
                    stop_start AS arrival_ts, stop_lat AS arrival_lat,
                    stop_lon AS arrival_lon,
                    eta, static_ts,
                    'speed' AS detection_method
                FROM stop_events
                WHERE stop_start >= static_ts - INTERVAL 1 DAY
                ORDER BY mmsi, dest_clean, eta, stop_start ASC
            """)
            n_spd = con.execute("SELECT COUNT(*) FROM arrivals_speed").fetchone()[0]
            print(f"   ⏸️  Arrêts >= {STOP_MIN_MINUTES}min: {n_spd}")
        else:
            con.execute("""
                CREATE TEMP TABLE arrivals_speed (
                    mmsi BIGINT, destination_clean VARCHAR,
                    port_lo_code VARCHAR, port_name VARCHAR,
                    port_lat DOUBLE, port_lon DOUBLE,
                    arrival_ts TIMESTAMPTZ, arrival_lat DOUBLE, arrival_lon DOUBLE,
                    eta TIMESTAMPTZ, static_ts TIMESTAMPTZ,
                    detection_method VARCHAR
                )
            """)

        con.execute("""
            CREATE TEMP TABLE arrivals_today AS
            SELECT
                mmsi, destination_clean, port_lo_code, port_name,
                port_lat, port_lon, arrival_ts, arrival_lat, arrival_lon,
                NULL::TIMESTAMPTZ AS departure_ts,
                NULL::DOUBLE AS departure_lat,
                NULL::DOUBLE AS departure_lon,
                eta, static_ts, detection_method,
                CAST(arrival_ts AS DATE) AS arrival_date
            FROM arrivals_geo
            UNION ALL
            SELECT
                a.mmsi, a.destination_clean, a.port_lo_code, a.port_name,
                a.port_lat, a.port_lon, a.arrival_ts, a.arrival_lat, a.arrival_lon,
                NULL::TIMESTAMPTZ, NULL::DOUBLE, NULL::DOUBLE,
                a.eta, a.static_ts, a.detection_method,
                CAST(a.arrival_ts AS DATE) AS arrival_date
            FROM arrivals_speed a
            WHERE NOT EXISTS (
                SELECT 1 FROM arrivals_geo g
                WHERE g.mmsi = a.mmsi
                  AND g.port_lo_code = a.port_lo_code
            )
        """)
    else:
        con.execute("""
            CREATE TEMP TABLE arrivals_today (
                mmsi BIGINT, destination_clean VARCHAR,
                port_lo_code VARCHAR, port_name VARCHAR,
                port_lat DOUBLE, port_lon DOUBLE,
                arrival_ts TIMESTAMPTZ, arrival_lat DOUBLE, arrival_lon DOUBLE,
                departure_ts TIMESTAMPTZ, departure_lat DOUBLE, departure_lon DOUBLE,
                eta TIMESTAMPTZ, static_ts TIMESTAMPTZ,
                detection_method VARCHAR,
                arrival_date DATE
            )
        """)

    n_today = con.execute("SELECT COUNT(*) FROM arrivals_today").fetchone()[0]

    if has_existing:
        n_existing = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{existing_path}')"
        ).fetchone()[0]
        print(f"   ♻️  Port calls existants: {n_existing:,}")
        try:
            con.execute(f"""
                CREATE TEMP TABLE port_calls_merged AS
                SELECT * FROM (
                    SELECT * FROM arrivals_today
                    UNION ALL
                    SELECT * FROM read_parquet('{existing_path}')
                )
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY mmsi, port_lo_code, arrival_ts
                    ORDER BY static_ts DESC
                ) = 1
            """)
        except Exception as e:
            print(f"   ⚠️ Merge échoué ({e}), fallback sans existant")
            con.execute("""
                CREATE TEMP TABLE port_calls_merged AS
                SELECT * FROM arrivals_today
            """)
    else:
        con.execute("""
            CREATE TEMP TABLE port_calls_merged AS
            SELECT * FROM arrivals_today
        """)

    if matched and n_pos > 0:
        con.execute(f"""
            CREATE TEMP TABLE departures_today AS
            SELECT DISTINCT ON (pc.mmsi, pc.port_lo_code)
                pc.mmsi, pc.port_lo_code,
                p.ts AS departure_ts,
                p.lat AS departure_lat,
                p.lon AS departure_lon
            FROM port_calls_merged pc
            JOIN (
                SELECT mmsi, ts, lat, lon, sog
                FROM read_parquet('{silver_file}')
                WHERE message_type IN ('PositionReport', 'ExtendedClassBPositionReport',
                                       'StandardClassBPositionReport')
                  AND lat IS NOT NULL AND lon IS NOT NULL
                  AND sog IS NOT NULL AND sog > 1.0
            ) p ON pc.mmsi = p.mmsi
            WHERE pc.departure_ts IS NULL
              AND p.ts > pc.arrival_ts
              AND haversine_km(p.lat, p.lon, pc.port_lat, pc.port_lon) > {PORT_RADIUS_KM}
            ORDER BY pc.mmsi, pc.port_lo_code, p.ts ASC
        """)
        n_dep = con.execute("SELECT COUNT(*) FROM departures_today").fetchone()[0]
        if n_dep > 0:
            print(f"   🚪 Départs détectés: {n_dep}")
            con.execute("""
                CREATE TEMP TABLE port_calls_final AS
                SELECT
                    m.mmsi, m.destination_clean, m.port_lo_code, m.port_name,
                    m.port_lat, m.port_lon,
                    m.arrival_ts, m.arrival_lat, m.arrival_lon,
                    COALESCE(m.departure_ts, d.departure_ts) AS departure_ts,
                    COALESCE(m.departure_lat, d.departure_lat) AS departure_lat,
                    COALESCE(m.departure_lon, d.departure_lon) AS departure_lon,
                    m.eta, m.static_ts, m.detection_method, m.arrival_date
                FROM port_calls_merged m
                LEFT JOIN departures_today d
                  ON m.mmsi = d.mmsi AND m.port_lo_code = d.port_lo_code
            """)
        else:
            con.execute("""
                CREATE TEMP TABLE port_calls_final AS
                SELECT * FROM port_calls_merged
            """)
    else:
        con.execute("""
            CREATE TEMP TABLE port_calls_final AS
            SELECT * FROM port_calls_merged
        """)

    n_final = con.execute("SELECT COUNT(*) FROM port_calls_final").fetchone()[0]
    con.execute(f"""
        COPY (SELECT * FROM port_calls_final ORDER BY mmsi, arrival_ts)
        TO '{port_calls_file}' (FORMAT 'PARQUET', COMPRESSION 'ZSTD')
    """)

    return port_calls_file, n_final
