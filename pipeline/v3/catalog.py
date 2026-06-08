"""Phase 5: DuckLake catalog management on S3."""

import os
import duckdb
from config import (
    BUCKET_PUBLIC, CATALOG_DIR, CATALOG_FILE, S3_CATALOG_KEY, S3_DATA_PREFIX,
    BASE_HTTPS, OVH_ENDPOINT, OVH_ACCESS_KEY, OVH_SECRET_KEY, s3_client,
)


def update_catalog(s3, uploaded_files, target_date, force,
                    output_dir_path='', tables=None):
    """
    Download catalog, register new files, upload catalog.
    Uses DuckLake with S3 DATA_PATH.
    """
    os.makedirs(CATALOG_DIR, exist_ok=True)
    if os.path.exists(CATALOG_FILE):
        os.remove(CATALOG_FILE)

    s3_data_path = f"s3://{BUCKET_PUBLIC}/{S3_DATA_PREFIX}/"

    is_new = False
    try:
        s3.download_file(BUCKET_PUBLIC, S3_CATALOG_KEY, CATALOG_FILE)
        print("   ✅ Catalogue récupéré via S3")
    except Exception:
        try:
            import requests
            r = requests.get(f"{BASE_HTTPS}/{S3_CATALOG_KEY}", timeout=10)
            if r.status_code == 200:
                with open(CATALOG_FILE, 'wb') as f:
                    f.write(r.content)
                print("   ✅ Catalogue récupéré via HTTPS")
            else:
                raise Exception(f"HTTP {r.status_code}")
        except Exception:
            is_new = True
            print("   🆕 Nouveau catalogue")

    con = duckdb.connect()
    con.execute("INSTALL httpfs; INSTALL ducklake; LOAD httpfs; LOAD ducklake;")
    con.execute(f"SET s3_endpoint='{OVH_ENDPOINT.replace('https://', '')}'")
    con.execute("SET s3_region='gra'")
    con.execute(f"SET s3_access_key_id='{OVH_ACCESS_KEY}'")
    con.execute(f"SET s3_secret_access_key='{OVH_SECRET_KEY}'")
    con.execute("SET s3_url_style='path'; SET s3_use_ssl=true")

    https_data_path = f"{BASE_HTTPS}/{S3_DATA_PREFIX}/"

    if is_new:
        con.execute(f"""
            ATTACH '{CATALOG_FILE}' AS ais_lake (
                TYPE ducklake, DATA_PATH '{https_data_path}',
                OVERRIDE_DATA_PATH true, AUTOMATIC_MIGRATION true
            )
        """)
        con.execute("DETACH ais_lake")
        print(f"💾 DATA_PATH public: {https_data_path}")

    con.execute(f"""
        ATTACH '{CATALOG_FILE}' AS ais_lake (
            TYPE ducklake, DATA_PATH '{s3_data_path}',
            OVERRIDE_DATA_PATH true
        )
    """)

    try:
        # Always ensure tables exist (idempotent: CREATE TABLE IF NOT EXISTS)
        _create_all_tables(con)
        if is_new:
            pass  # tables just created above
        else:
            if force:
                _clean_partitions(con, target_date, tables)
            _migrate_schema(con, tables)

        known = {
            row[0] for row in con.execute(
                "SELECT path FROM __ducklake_metadata_ais_lake.ducklake_data_file"
            ).fetchall()
        }

        file_registry = _classify_files(uploaded_files, output_dir_path)

        for local_path, url in uploaded_files:
            info = file_registry.get(local_path)
            if not info:
                continue
            table_name, hive = info
            if tables is not None and table_name not in tables:
                continue

            # Single-file tables (overwritten each run): always re-register.
            # Partitioned tables: skip if URL already known (unless --force).
            overwrite_tables = {'vessels', 'port_calls', 'port_congestion'}
            if table_name not in overwrite_tables and url in known and not force:
                continue

            ignore = 'true' if table_name == 'messages' else 'false'

            # Delete old registration for single-file tables before re-adding
            if table_name in overwrite_tables and url in known:
                try:
                    con.execute(
                        "DELETE FROM __ducklake_metadata_ais_lake.ducklake_data_file "
                        "WHERE table_id = (SELECT table_id FROM "
                        "__ducklake_metadata_ais_lake.ducklake_table "
                        f"WHERE table_name = '{table_name}')"
                    )
                except Exception:
                    pass
            con.execute(
                f"CALL ducklake_add_data_files('ais_lake', '{table_name}', "
                f"'{url}', hive_partitioning={str(hive).lower()}, "
                f"ignore_extra_columns={ignore})"
            )
            print(f"   📋 {table_name}: {url}")

    finally:
        con.close()

    s3.upload_file(CATALOG_FILE, BUCKET_PUBLIC, S3_CATALOG_KEY,
                   ExtraArgs={"ACL": "public-read"})
    print(f"   ✅ Catalogue publié → {BASE_HTTPS}/{S3_CATALOG_KEY}")


def _classify_files(uploaded_files, output_dir_path):
    mapping = {}
    for local_path, _ in uploaded_files:
        rel = os.path.relpath(local_path, output_dir_path)
        if rel.startswith('silver/'):
            mapping[local_path] = ('messages', True)
        elif rel.startswith('gold/vessels_positions/'):
            mapping[local_path] = ('vessels_positions', True)
        elif rel.startswith('gold/vessel_tracks/'):
            mapping[local_path] = ('vessel_tracks', True)
        elif rel.startswith('gold/base_stations/'):
            mapping[local_path] = ('base_stations', True)
        elif rel.startswith('gold/aids_to_navigation/'):
            mapping[local_path] = ('aids_to_navigation', True)
        elif rel.startswith('gold/vessels/'):
            mapping[local_path] = ('vessels', False)
        elif rel.startswith('gold/port_calls/'):
            mapping[local_path] = ('port_calls', False)
        elif rel.startswith('gold/port_congestion/'):
            mapping[local_path] = ('port_congestion', False)
    return mapping


def _create_all_tables(con):
    # Force-recreate new tables that may have wrong schema from previous failed runs
    for tbl in ('port_calls', 'port_congestion'):
        try:
            con.execute(f"DROP TABLE IF EXISTS ais_lake.{tbl}")
        except Exception:
            pass

    tables = [
        ("messages", """
            CREATE TABLE IF NOT EXISTS ais_lake.messages (
                message_type VARCHAR, mmsi BIGINT, ts TIMESTAMPTZ,
                lat DOUBLE, lon DOUBLE, received_at TIMESTAMPTZ,
                source_listener VARCHAR,
                sog DOUBLE, cog DOUBLE, true_heading INTEGER,
                navigational_status INTEGER, rate_of_turn INTEGER,
                message_id INTEGER, position_accuracy BOOLEAN,
                raim BOOLEAN, valid BOOLEAN,
                name VARCHAR, call_sign VARCHAR,
                imo_number BIGINT, ship_type INTEGER, ais_version INTEGER,
                length DOUBLE, width DOUBLE,
                dimension_a DOUBLE, dimension_b DOUBLE,
                dimension_c DOUBLE, dimension_d DOUBLE,
                max_static_draught DOUBLE,
                destination VARCHAR, eta TIMESTAMPTZ, dte BOOLEAN,
                fix_type INTEGER, type_of_aton INTEGER,
                off_position BOOLEAN, virtual_aton BOOLEAN,
                raw_message VARCHAR, metadata_json VARCHAR,
                year INTEGER, month INTEGER, day INTEGER
            )
        """),
        ("vessels_positions", """
            CREATE TABLE IF NOT EXISTS ais_lake.vessels_positions (
                message_type VARCHAR, mmsi BIGINT, ts TIMESTAMPTZ,
                lat DOUBLE, lon DOUBLE, received_at TIMESTAMPTZ,
                source_listener VARCHAR,
                sog DOUBLE, cog DOUBLE, true_heading INTEGER,
                navigational_status INTEGER, rate_of_turn INTEGER,
                message_id INTEGER, position_accuracy BOOLEAN,
                raim BOOLEAN, valid BOOLEAN,
                year INTEGER, month INTEGER, day INTEGER
            )
        """),
        ("vessel_tracks", """
            CREATE TABLE IF NOT EXISTS ais_lake.vessel_tracks (
                mmsi INTEGER, ts INTEGER, lat INTEGER, lon INTEGER,
                heading INTEGER, date DATE
            )
        """),
        ("base_stations", """
            CREATE TABLE IF NOT EXISTS ais_lake.base_stations (
                mmsi BIGINT, ts TIMESTAMPTZ, lat DOUBLE, lon DOUBLE,
                received_at TIMESTAMPTZ, source_listener VARCHAR,
                message_id INTEGER, raim BOOLEAN,
                year INTEGER, month INTEGER, day INTEGER
            )
        """),
        ("aids_to_navigation", """
            CREATE TABLE IF NOT EXISTS ais_lake.aids_to_navigation (
                mmsi BIGINT, name VARCHAR, type_of_aton INTEGER,
                ts TIMESTAMPTZ, lat DOUBLE, lon DOUBLE,
                dimension_a DOUBLE, dimension_b DOUBLE,
                dimension_c DOUBLE, dimension_d DOUBLE,
                off_position BOOLEAN, virtual_aton BOOLEAN, raim BOOLEAN,
                received_at TIMESTAMPTZ, source_listener VARCHAR,
                year INTEGER, month INTEGER, day INTEGER
            )
        """),
        ("vessels", """
            CREATE TABLE IF NOT EXISTS ais_lake.vessels (
                mmsi BIGINT, name VARCHAR, call_sign VARCHAR,
                imo_number BIGINT, ship_type INTEGER,
                length DOUBLE, width DOUBLE, destination VARCHAR,
                last_seen_static TIMESTAMPTZ
            )
        """),
        ("port_calls", """
            CREATE TABLE IF NOT EXISTS ais_lake.port_calls (
                mmsi BIGINT, destination_clean VARCHAR,
                port_lo_code VARCHAR, port_name VARCHAR,
                port_lat DOUBLE, port_lon DOUBLE,
                arrival_ts TIMESTAMPTZ, arrival_lat DOUBLE, arrival_lon DOUBLE,
                departure_ts TIMESTAMPTZ, departure_lat DOUBLE, departure_lon DOUBLE,
                eta TIMESTAMPTZ, static_ts TIMESTAMPTZ,
                detection_method VARCHAR, arrival_date DATE
            )
        """),
        ("port_congestion", """
            CREATE TABLE IF NOT EXISTS ais_lake.port_congestion (
                port_lo_code VARCHAR, hour TIMESTAMPTZ,
                vessels_in_port BIGINT, arrivals BIGINT, departures BIGINT,
                date DATE
            )
        """),
    ]
    partitions = {
        "messages":              "year, month, day",
        "vessels_positions":     "year, month, day",
        "vessel_tracks":         "date",
        "base_stations":         "year, month, day",
        "aids_to_navigation":    "year, month, day",
    }

    for table_name, create_sql in tables:
        try:
            con.execute(create_sql)
        except Exception as e:
            print(f"   ⚠️ CREATE {table_name}: {e}")

    for table_name, cols in partitions.items():
        try:
            con.execute(
                f"ALTER TABLE ais_lake.{table_name} "
                f"SET PARTITIONED BY ({cols})"
            )
        except Exception:
            pass

    print("   🗂️  Tables créées")


def _migrate_schema(con, tables=None):
    if tables is None or 'vessel_tracks' in tables:
        try:
            con.execute(
                "ALTER TABLE ais_lake.vessel_tracks "
                "ADD COLUMN IF NOT EXISTS heading INTEGER"
            )
        except Exception:
            pass


def _clean_partitions(con, target_date, tables=None):
    all_tables = tables is None
    y, m, d = target_date.year, f"{target_date.month:02d}", target_date.day
    date_str = target_date.strftime('%Y-%m-%d')

    patterns = {
        'messages':              f"%silver/year={y}/month={m}/day={d}/%",
        'vessels_positions':     f"%vessels_positions/year={y}/month={m}/day={d}/%",
        'vessel_tracks':         f"%vessel_tracks/date={date_str}/%",
        'base_stations':         f"%base_stations/year={y}/month={m}/day={d}/%",
        'aids_to_navigation':    f"%aids_to_navigation/year={y}/month={m}/day={d}/%",
        'vessels':               f"%vessels/vessels.parquet%",
        'port_calls':            f"%port_calls/port_calls.parquet%",
        'port_congestion':       f"%port_congestion/port_congestion.parquet%",
    }
    for table_name, path_pattern in patterns.items():
        if not all_tables and table_name not in (tables or set()):
            continue
        try:
            result = con.execute(f"""
                DELETE FROM __ducklake_metadata_ais_lake.ducklake_data_file
                WHERE table_id = (
                    SELECT table_id FROM __ducklake_metadata_ais_lake.ducklake_table
                    WHERE table_name = '{table_name}'
                )
                AND path LIKE '{path_pattern}'
            """)
            deleted = result.fetchone()[0] if result else 0
            if deleted > 0:
                print(f"   🗑️  {table_name}: {deleted} fichier(s) "
                      f"(partition {date_str})")
        except Exception as e:
            print(f"   ⚠️ DELETE {table_name}: {e}")
