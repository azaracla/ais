import duckdb
import boto3
import os
import tempfile
from datetime import datetime, timedelta, timezone
from configuration import *
from botocore.client import Config

def fetch_raw_files_window():
   """Récupère récursivement les URIs S3 des dossiers bruts de la fenêtre glissante J-7 à J"""
   s3 = boto3.client(
       's3',
       endpoint_url=OVH_ENDPOINT,
       aws_access_key_id=OVH_ACCESS_KEY,
       aws_secret_access_key=OVH_SECRET_KEY,
       region_name=OVH_REGION,
       #config=Config(s3={'addressing_style': 'path'})
   )
   
   today = datetime.now(timezone.utc)
   paths = []
   
   # Parcours des 8 derniers jours pour capturer les messages satellitaires différés (Late-Arriving)
   for i in range(8):
       date_target = today - timedelta(days=i)
       prefix = f"raw/year={date_target.year}/month={date_target.month:02d}/day={date_target.day:02d}/"
       res = s3.list_objects_v2(Bucket=BUCKET_RAW, Prefix=prefix)
       if "Contents" in res:
           for obj in res["Contents"]:
               paths.append(f"s3://{BUCKET_RAW}/{obj['Key']}")
   return paths

def run_consolidation():
   yesterday_date = datetime.now(timezone.utc) - timedelta(days=1)
   yesterday = yesterday_date.strftime('%Y-%m-%d')
   print(f"🧹 Début de la consolidation globale et du compactage pour la date : {yesterday}")
   
   files = fetch_raw_files_window()
   if not files:
       print("⚠️ Aucun fichier brut d'ingestion identifié sur la fenêtre temporelle.")
       return
       
   # 1. Créer un répertoire temporaire LOCAL pour DuckLake
   with tempfile.TemporaryDirectory() as tmpdir:
       local_ducklake_dir = os.path.join(tmpdir, "metadata")
       os.makedirs(local_ducklake_dir, exist_ok=True)
       local_ducklake = os.path.join(local_ducklake_dir, "metadata.ducklake")
       s3_ducklake_key = "metadata.ducklake"
       
       # 2. Configurer le client S3
       s3 = boto3.client(
           's3',
           endpoint_url=OVH_ENDPOINT,
           aws_access_key_id=OVH_ACCESS_KEY,
           aws_secret_access_key=OVH_SECRET_KEY,
           region_name=OVH_REGION,
           #config=Config(s3={'addressing_style': 'path'})
       )
       
       # 3. Télécharger le DuckLake existant SI il existe sur S3
       ducklake_exists = False
       try:
           # Essayer directement le téléchargement
           s3.download_file(BUCKET_PUBLIC, s3_ducklake_key, local_ducklake)
           print("📥 DuckLake existant téléchargé localement")
           ducklake_exists = True
       except Exception as e:
           # Si le fichier n'existe pas ou autre erreur, on crée un nouveau DuckLake
           print(f"🆕 Nouveau DuckLake - création locale (raison: {type(e).__name__})")
           ducklake_exists = False
       
       # 4. Configurer DuckDB
       con = duckdb.connect()
       con.execute("INSTALL httpfs; INSTALL ducklake; LOAD httpfs; LOAD ducklake;")
       
       clean_endpoint = OVH_ENDPOINT.replace("https://", "")
       con.execute(f"SET s3_endpoint = '{clean_endpoint}'")
       con.execute("SET s3_region = 'us-east-1'")  # Région générique pour OVHcloud
       con.execute(f"SET s3_access_key_id = '{OVH_ACCESS_KEY}'")
       con.execute(f"SET s3_secret_access_key = '{OVH_SECRET_KEY}'")
       con.execute("SET s3_url_style = 'path'")
       con.execute("SET s3_use_ssl = true")
       
       # DATA_PATH pointe directement vers S3 - DuckLake écrira les Parquet sur S3
       data_uri = f"s3://{BUCKET_PUBLIC}/data/"
       
       # 5. Attacher le DuckLake avec DATA_PATH S3
       if ducklake_exists:
           print("🔗 Attachement du DuckLake...")
           con.execute(f"ATTACH '{local_ducklake}' AS public_lake (TYPE ducklake, DATA_PATH '{data_uri}')")
       else:
           print("🆕 Création du DuckLake...")
           con.execute(f"ATTACH '{local_ducklake}' AS public_lake (TYPE ducklake, DATA_PATH '{data_uri}')")
       
       # 6. Vérifier/créer les tables (Sans PRIMARY KEY - non supporté par DuckLake)
       con.execute("""
           CREATE TABLE IF NOT EXISTS public_lake.positions (
               id VARCHAR,
               mmsi BIGINT, name VARCHAR, timestamp TIMESTAMPTZ,
               lat DOUBLE, lon DOUBLE, sog DOUBLE, cog DOUBLE,
               true_heading INTEGER, navigational_status INTEGER,
               rate_of_turn INTEGER, message_type VARCHAR,
               message_id INTEGER, position_accuracy BOOLEAN,
               raim BOOLEAN, valid BOOLEAN, received_at TIMESTAMPTZ,
               source_listener VARCHAR
           )
       """)
       con.execute("""
           CREATE TABLE IF NOT EXISTS public_lake.ship_static (
               mmsi BIGINT, name VARCHAR, call_sign VARCHAR,
               imo_number BIGINT, ship_type INTEGER, ais_version INTEGER,
               length DOUBLE, width DOUBLE, dimension_a DOUBLE,
               dimension_b DOUBLE, dimension_c DOUBLE, dimension_d DOUBLE,
               max_static_draught DOUBLE, destination VARCHAR,
               eta TIMESTAMP, dte BOOLEAN, fix_type INTEGER,
               last_updated TIMESTAMPTZ, source_listener VARCHAR
           )
       """)
       con.execute("""
           CREATE TABLE IF NOT EXISTS public_lake.base_stations (
               mmsi BIGINT, station_name VARCHAR,
               lat DOUBLE, lon DOUBLE, timestamp TIMESTAMPTZ,
               raim BOOLEAN, last_updated TIMESTAMPTZ
           )
       """)
       con.execute("""
           CREATE TABLE IF NOT EXISTS public_lake.aids_to_navigation (
               mmsi BIGINT, name VARCHAR, type_of_aton INTEGER,
               lat DOUBLE, lon DOUBLE, timestamp TIMESTAMPTZ,
               dimension_a DOUBLE, dimension_b DOUBLE, dimension_c DOUBLE,
               dimension_d DOUBLE, off_position BOOLEAN, virtual_aton BOOLEAN,
               raim BOOLEAN, last_updated TIMESTAMPTZ
           )
       """)
       con.execute("""
           CREATE TABLE IF NOT EXISTS public_lake.unstructured_messages (
               id BIGINT,
               mmsi BIGINT, timestamp TIMESTAMPTZ, message_type VARCHAR,
               raw_message JSON, received_at TIMESTAMPTZ
           )
       """)

       # Enregistrement défensif des fichiers NDJSON.zst via read_json_auto de DuckDB
       # DuckDB ne supporte pas les paramètres préparés dans CREATE VIEW, donc on construit la liste
       files_str = ", ".join([f"'{f}'" for f in files])
       con.execute(f"CREATE OR REPLACE TEMP VIEW raw_view AS SELECT * FROM read_json_auto([{files_str}], format='newline_delimited', compression='zstd', maximum_object_size=10000000, ignore_errors=true)")

       # 1. Positions de navigation (Traitement de l'Event Time hier, déduplication et routage du Message Type 27)
       for hour_start in range(0, 24, 3):
           h_start = f"{hour_start:02d}:00:00"
           h_end = f"{(hour_start+2):02d}:59:59"
           print(f"⏳ Compactage incrémental colonnaire de la tranche horaire {h_start} à {h_end}...")
           
           con.execute(f"""
               CREATE OR REPLACE TEMP TABLE stage_positions AS
               SELECT
                   md5(concat_ws('|', 
                       CAST(json_extract_string(metadata, '$.MMSI') AS INTEGER), 
                       CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ),
                       COALESCE(CAST(json_extract_string(metadata, '$.latitude') AS DOUBLE), CAST(json_extract_string(metadata, '$.Latitude') AS DOUBLE)),
                       COALESCE(CAST(json_extract_string(metadata, '$.longitude') AS DOUBLE), CAST(json_extract_string(metadata, '$.Longitude') AS DOUBLE)),
                       COALESCE(
                           CAST(json_extract_string(message, '$.PositionReport.MessageID') AS INTEGER),
                           CAST(json_extract_string(message, '$.ExtendedClassBPositionReport.MessageID') AS INTEGER),
                           CAST(json_extract_string(message, '$.StandardClassBPositionReport.MessageID') AS INTEGER),
                           CAST(json_extract_string(message, '$.LongRangeAisBroadcastMessage.MessageID') AS INTEGER)
                       )
                   )) AS id,
                   CAST(json_extract_string(metadata, '$.MMSI') AS INTEGER) AS mmsi,
                   json_extract_string(metadata, '$.ShipName') AS name,
                   CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) AS timestamp,
                   COALESCE(CAST(json_extract_string(metadata, '$.latitude') AS DOUBLE), CAST(json_extract_string(metadata, '$.Latitude') AS DOUBLE)) AS lat,
                   COALESCE(CAST(json_extract_string(metadata, '$.longitude') AS DOUBLE), CAST(json_extract_string(metadata, '$.Longitude') AS DOUBLE)) AS lon,
                   COALESCE(
                       CAST(json_extract_string(message, '$.PositionReport.Sog') AS DOUBLE),
                       CAST(json_extract_string(message, '$.ExtendedClassBPositionReport.Sog') AS DOUBLE),
                       CAST(json_extract_string(message, '$.StandardClassBPositionReport.Sog') AS DOUBLE),
                       CAST(json_extract_string(message, '$.LongRangeAisBroadcastMessage.Sog') AS DOUBLE)
                   ) AS sog,
                   COALESCE(
                       CAST(json_extract_string(message, '$.PositionReport.Cog') AS DOUBLE),
                       CAST(json_extract_string(message, '$.ExtendedClassBPositionReport.Cog') AS DOUBLE),
                       CAST(json_extract_string(message, '$.StandardClassBPositionReport.Cog') AS DOUBLE),
                       CAST(json_extract_string(message, '$.LongRangeAisBroadcastMessage.Cog') AS DOUBLE)
                   ) AS cog,
                   COALESCE(
                       CAST(json_extract_string(message, '$.PositionReport.TrueHeading') AS INTEGER),
                       CAST(json_extract_string(message, '$.ExtendedClassBPositionReport.TrueHeading') AS INTEGER),
                       CAST(json_extract_string(message, '$.StandardClassBPositionReport.TrueHeading') AS INTEGER),
                       CAST(json_extract_string(message, '$.LongRangeAisBroadcastMessage.TrueHeading') AS INTEGER)
                   ) AS true_heading,
                   COALESCE(
                       CAST(json_extract_string(message, '$.PositionReport.NavigationalStatus') AS INTEGER),
                       CAST(json_extract_string(message, '$.LongRangeAisBroadcastMessage.NavigationalStatus') AS INTEGER)
                   ) AS navigational_status,
                   COALESCE(
                       CAST(json_extract_string(message, '$.PositionReport.RateOfTurn') AS INTEGER),
                       CAST(json_extract_string(message, '$.ExtendedClassBPositionReport.RateOfTurn') AS INTEGER)
                   ) AS rate_of_turn,
                   message_type,
                   COALESCE(
                       CAST(json_extract_string(message, '$.PositionReport.MessageID') AS INTEGER),
                       CAST(json_extract_string(message, '$.ExtendedClassBPositionReport.MessageID') AS INTEGER),
                       CAST(json_extract_string(message, '$.StandardClassBPositionReport.MessageID') AS INTEGER),
                       CAST(json_extract_string(message, '$.LongRangeAisBroadcast.MessageID') AS INTEGER)
                   ) AS message_id,
                   COALESCE(
                       CAST(json_extract_string(message, '$.PositionReport.PositionAccuracy') AS BOOLEAN),
                       CAST(json_extract_string(message, '$.ExtendedClassBPositionReport.PositionAccuracy') AS BOOLEAN),
                       CAST(json_extract_string(message, '$.StandardClassBPositionReport.PositionAccuracy') AS BOOLEAN),
                       CAST(json_extract_string(message, '$.LongRangeAisBroadcast.PositionAccuracy') AS BOOLEAN)
                   ) AS position_accuracy,
                   COALESCE(
                       CAST(json_extract_string(message, '$.PositionReport.Raim') AS BOOLEAN),
                       CAST(json_extract_string(message, '$.ExtendedClassBPositionReport.Raim') AS BOOLEAN),
                       CAST(json_extract_string(message, '$.StandardClassBPositionReport.Raim') AS BOOLEAN),
                       CAST(json_extract_string(message, '$.LongRangeAisBroadcast.Raim') AS BOOLEAN)
                   ) AS raim,
                   COALESCE(
                       CAST(json_extract_string(message, '$.PositionReport.Valid') AS BOOLEAN),
                       CAST(json_extract_string(message, '$.ExtendedClassBPositionReport.Valid') AS BOOLEAN),
                       CAST(json_extract_string(message, '$.StandardClassBPositionReport.Valid') AS BOOLEAN),
                       CAST(json_extract_string(message, '$.LongRangeAisBroadcast.Valid') AS BOOLEAN)
                   ) AS valid,
                   CAST(received_at AS TIMESTAMPTZ) AS received_at,
                   listener_id AS source_listener
               FROM raw_view
               WHERE message_type IN ('PositionReport', 'ExtendedClassBPositionReport', 'StandardClassBPositionReport', 'LongRangeAisBroadcast')
                 AND CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) BETWEEN '{yesterday} {h_start}' AND '{yesterday} {h_end}'
               QUALIFY ROW_NUMBER() OVER (
                   PARTITION BY mmsi, timestamp, lat, lon
                   ORDER BY received_at DESC
               ) = 1
               ORDER BY mmsi, timestamp;
           """)
           # INSERT avec déduplication (DuckLake ne supporte pas INSERT OR IGNORE)
           con.execute("""
               INSERT INTO public_lake.positions
               SELECT * FROM stage_positions
               WHERE (id) NOT IN (SELECT id FROM public_lake.positions)
           """)

       # 2. Informations statiques (Classe A & Classe B polymorphe type 24)
       print("⏳ Fusion incrémentale de la flotte de commerce et de plaisance...")
       con.execute(f"""
           CREATE OR REPLACE TEMP TABLE stage_static AS
           WITH union_static AS (
               SELECT
                   CAST(json_extract_string(metadata, '$.MMSI') AS INTEGER) AS mmsi,
                   COALESCE(
                       json_extract_string(message, '$.ShipStaticData.Name'), 
                       json_extract_string(message, '$.StaticDataReport.ReportA.Name'), 
                       json_extract_string(metadata, '$.ShipName')
                   ) AS name,
                   COALESCE(
                       json_extract_string(message, '$.ShipStaticData.CallSign'),
                       json_extract_string(message, '$.StaticDataReport.ReportB.CallSign')
                   ) AS call_sign,
                   CAST(json_extract_string(message, '$.ShipStaticData.ImoNumber') AS INTEGER) AS imo_number,
                   COALESCE(
                       CAST(json_extract_string(message, '$.ShipStaticData.Type') AS INTEGER),
                       CAST(json_extract_string(message, '$.StaticDataReport.ReportB.ShipType') AS INTEGER)
                   ) AS ship_type,
                   CAST(json_extract_string(message, '$.ShipStaticData.AisVersion') AS INTEGER) AS ais_version,
                   COALESCE(
                       (CAST(json_extract_string(message, '$.ShipStaticData.Dimension.A') AS DOUBLE) + CAST(json_extract_string(message, '$.ShipStaticData.Dimension.B') AS DOUBLE))::DOUBLE,
                       (CAST(json_extract_string(message, '$.StaticDataReport.ReportB.Dimension.A') AS DOUBLE) + CAST(json_extract_string(message, '$.StaticDataReport.ReportB.Dimension.B') AS DOUBLE))::DOUBLE
                   ) AS length,
                   COALESCE(
                       (CAST(json_extract_string(message, '$.ShipStaticData.Dimension.C') AS DOUBLE) + CAST(json_extract_string(message, '$.ShipStaticData.Dimension.D') AS DOUBLE))::DOUBLE,
                       (CAST(json_extract_string(message, '$.StaticDataReport.ReportB.Dimension.C') AS DOUBLE) + CAST(json_extract_string(message, '$.StaticDataReport.ReportB.Dimension.D') AS DOUBLE))::DOUBLE
                   ) AS width,
                   COALESCE(
                       CAST(json_extract_string(message, '$.ShipStaticData.Dimension.A') AS DOUBLE),
                       CAST(json_extract_string(message, '$.StaticDataReport.ReportB.Dimension.A') AS DOUBLE)
                   ) AS dimension_a,
                   COALESCE(
                       CAST(json_extract_string(message, '$.ShipStaticData.Dimension.B') AS DOUBLE),
                       CAST(json_extract_string(message, '$.StaticDataReport.ReportB.Dimension.B') AS DOUBLE)
                   ) AS dimension_b,
                   COALESCE(
                       CAST(json_extract_string(message, '$.ShipStaticData.Dimension.C') AS DOUBLE),
                       CAST(json_extract_string(message, '$.StaticDataReport.ReportB.Dimension.C') AS DOUBLE)
                   ) AS dimension_c,
                   COALESCE(
                       CAST(json_extract_string(message, '$.ShipStaticData.Dimension.D') AS DOUBLE),
                       CAST(json_extract_string(message, '$.StaticDataReport.ReportB.Dimension.D') AS DOUBLE)
                   ) AS dimension_d,
                   CAST(json_extract_string(message, '$.ShipStaticData.MaximumStaticDraught') AS DOUBLE) AS max_static_draught,
                   json_extract_string(message, '$.ShipStaticData.Destination') AS destination,
                   CASE WHEN CAST(json_extract_string(message, '$.ShipStaticData.Eta.Month') AS INTEGER) BETWEEN 1 AND 12 
                        AND CAST(json_extract_string(message, '$.ShipStaticData.Eta.Day') AS INTEGER) BETWEEN 1 AND 31 THEN
                        MAKE_TIMESTAMP(
                           YEAR(CURRENT_DATE), 
                           CAST(json_extract_string(message, '$.ShipStaticData.Eta.Month') AS INTEGER), 
                           CAST(json_extract_string(message, '$.ShipStaticData.Eta.Day') AS INTEGER), 
                           COALESCE(NULLIF(CAST(json_extract_string(message, '$.ShipStaticData.Eta.Hour') AS INTEGER), 24), 0), 
                           COALESCE(NULLIF(CAST(json_extract_string(message, '$.ShipStaticData.Eta.Minute') AS INTEGER), 60), 0), 
                           0
                        )
                   ELSE NULL END AS eta,
                   CAST(json_extract_string(message, '$.ShipStaticData.Dte') AS BOOLEAN) AS dte,
                   CAST(json_extract_string(message, '$.ShipStaticData.FixType') AS INTEGER) AS fix_type,
                   CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) AS last_updated,
                   listener_id AS source_listener
               FROM raw_view
               WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
                 AND CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) BETWEEN '{yesterday} 00:00:00' AND '{yesterday} 23:59:59'
           )
           SELECT * FROM union_static
           QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY last_updated DESC) = 1;
       """)
       # INSERT OR REPLACE -> DELETE + INSERT (DuckLake ne supporte pas OR REPLACE)
       con.execute("DELETE FROM public_lake.ship_static WHERE mmsi IN (SELECT mmsi FROM stage_static)")
       con.execute("INSERT INTO public_lake.ship_static SELECT * FROM stage_static")

       # 3. Stations de Base (Traitement exclusif du Type 4 pour découpler l'infrastructure)
       print("⏳ Consolidation des rapports des Stations de Base fixes (Type 4)...")
       con.execute(f"""
           CREATE OR REPLACE TEMP TABLE stage_stations AS
           SELECT
               CAST(json_extract_string(metadata, '$.MMSI') AS INTEGER) AS mmsi,
               json_extract_string(metadata, '$.ShipName') AS station_name,
               COALESCE(CAST(json_extract_string(metadata, '$.latitude') AS DOUBLE), CAST(json_extract_string(metadata, '$.Latitude') AS DOUBLE)) AS lat,
               COALESCE(CAST(json_extract_string(metadata, '$.longitude') AS DOUBLE), CAST(json_extract_string(metadata, '$.Longitude') AS DOUBLE)) AS lon,
               CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) AS timestamp,
               CAST(json_extract_string(message, '$.BaseStationReport.Raim') AS BOOLEAN) AS raim,
               CAST(received_at AS TIMESTAMPTZ) AS last_updated
           FROM raw_view
           WHERE message_type = 'BaseStationReport'
             AND CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) BETWEEN '{yesterday} 00:00:00' AND '{yesterday} 23:59:59'
           QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY timestamp DESC) = 1;
       """)
       con.execute("DELETE FROM public_lake.base_stations WHERE mmsi IN (SELECT mmsi FROM stage_stations)")
       con.execute("INSERT INTO public_lake.base_stations SELECT * FROM stage_stations")

       # 4. Balises et phares (ATON - Message 21)
       print("⏳ Compactage et synchronisation des aides de navigation (ATON)...")
       con.execute(f"""
           CREATE OR REPLACE TEMP TABLE stage_aton AS
           SELECT
               CAST(json_extract_string(metadata, '$.MMSI') AS INTEGER) AS mmsi,
               json_extract_string(message, '$.AidsToNavigationReport.Name') AS name,
               CAST(json_extract_string(message, '$.AidsToNavigationReport.Type') AS INTEGER) AS type_of_aton,
               COALESCE(CAST(json_extract_string(metadata, '$.latitude') AS DOUBLE), CAST(json_extract_string(metadata, '$.Latitude') AS DOUBLE)) AS lat,
               COALESCE(CAST(json_extract_string(metadata, '$.longitude') AS DOUBLE), CAST(json_extract_string(metadata, '$.Longitude') AS DOUBLE)) AS lon,
               CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) AS timestamp,
               CAST(json_extract_string(message, '$.AidsToNavigationReport.Dimension.A') AS DOUBLE) AS dimension_a,
               CAST(json_extract_string(message, '$.AidsToNavigationReport.Dimension.B') AS DOUBLE) AS dimension_b,
               CAST(json_extract_string(message, '$.AidsToNavigationReport.Dimension.C') AS DOUBLE) AS dimension_c,
               CAST(json_extract_string(message, '$.AidsToNavigationReport.Dimension.D') AS DOUBLE) AS dimension_d,
               CAST(json_extract_string(message, '$.AidsToNavigationReport.OffPosition') AS BOOLEAN) AS off_position,
               CAST(json_extract_string(message, '$.AidsToNavigationReport.VirtualAidsToNavigation') AS BOOLEAN) AS virtual_aton,
               CAST(json_extract_string(message, '$.AidsToNavigationReport.Raim') AS BOOLEAN) AS raim,
               CAST(received_at AS TIMESTAMPTZ) AS last_updated
           FROM raw_view
           WHERE message_type = 'AidsToNavigationReport'
             AND CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) BETWEEN '{yesterday} 00:00:00' AND '{yesterday} 23:59:59'
           QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY timestamp DESC) = 1;
       """)
       con.execute("DELETE FROM public_lake.aids_to_navigation WHERE mmsi IN (SELECT mmsi FROM stage_aton)")
       con.execute("INSERT INTO public_lake.aids_to_navigation SELECT * FROM stage_aton")

       # 5. Messages techniques et de sécurité maritime restants
       print("⏳ Compactage et archivage des messages binaires et d'alerte...")
       con.execute(f"""
           INSERT INTO public_lake.unstructured_messages (mmsi, timestamp, message_type, raw_message, received_at)
           SELECT
               CAST(json_extract_string(metadata, '$.MMSI') AS INTEGER) AS mmsi,
               CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) AS timestamp,
               message_type,
               message AS raw_message,
               CAST(received_at AS TIMESTAMPTZ) AS received_at
           FROM raw_view
           WHERE message_type NOT IN ('PositionReport', 'ExtendedClassBPositionReport', 'StandardClassBPositionReport', 'LongRangeAisBroadcast', 'ShipStaticData', 'StaticDataReport', 'BaseStationReport', 'AidsToNavigationReport')
             AND CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) BETWEEN '{yesterday} 00:00:00' AND '{yesterday} 23:59:59';
       """)

       # 7. Exporter le metadata DuckLake mis à jour
       print("💾 Export du catalogue DuckLake mis à jour...")
       con.execute(f"EXPORT DATABASE public_lake TO '{local_ducklake_dir}'")
       
       # 8. Uploader le metadata sur S3
       # Les données Parquet sont déjà sur S3 (écrites par DuckLake via DATA_PATH)
       exported_file = os.path.join(local_ducklake_dir, "metadata.ducklake")
       s3.upload_file(exported_file, BUCKET_PUBLIC, s3_ducklake_key)
       print("📤 DuckLake complet (metadata + données) uploadé sur S3")
       
       con.close()
   
   print("🎉 Consolidation globale et compactage sur OVHcloud terminés avec succès !")

if __name__ == "__main__":
   run_consolidation()
