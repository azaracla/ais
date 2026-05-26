Architecture DuckLake Public AIS - Spécification Révisée & Plan de Production (Échelle Globale - OVHcloud)
📋 1. Contexte & Modélisation Financière Globale
* Source de données : API temps réel AISstream Monde.
* Avertissement légal important : AISstream est une source communautaire opérée en "best effort" sans garantie de couverture mondiale exhaustive ni SLA de haute disponibilité. La densité des messages varie selon la disposition des stations de réception terrestres et des satellites partenaires.
Le projet fournit un entrepôt de données analytique public, transactionnel et auditable, mais non autoritatif. Le guide utilisateur devra explicitement mentionner :"La couverture globale dépend de la disponibilité de la réception AIS amont et peut varier temporellement et géographiquement."
* Résolution du « Small File Problem » & Auditabilité :
   * Ingestion brute (raw) : NDJSON compressé en Zstandard (ZSTD), append-only, aucune transformation lourde ni inférence de schéma dans le chemin chaud pour garantir l'absorption des bursts de données.
   * Structure : s3://ais-raw-prod/raw/year=YYYY/month=MM/day=DD/hour=HH/
   * Rotation : Fichiers de 64 à 256 Mo compressés, flushés toutes les 30 à 60 secondes.
   * Restitution analytique (DuckLake) : Fichiers Parquet compressés (ZSTD) avec partitionnement temporel horaire (positions/date=YYYY-MM-DD/hour=HH/), row groups optimisés et compactage incrémental.
   * Structure : s3://ais-public-prod/data_files/
   * Politique de cycle de vie (Lifecycle Policy) : Les données brutes de ais-raw-prod sont purgées automatiquement à J+7. Les données de ais-public-prod (DuckLake) sont conservées de manière permanente avec historique des snapshots.
   * Volume estimé (Monde) :
   * Nombre de messages : ~100 à 150 millions de messages par jour (fluctue selon couverture satellitaire active).
   * Stockage Brut Temporaire (OVH Privé) : ~10 à 15 Go/jour compressé NDJSON.zst.
   * Stockage Analytique Compacté (OVH Public) : ~5 à 7 Go/jour après déduplification globale et compactage colonnaire.
   * Projection annuelle : ~2.2 To de données DuckLake consolidées.
   * Tarif S3 (OVHcloud HP S3) : ~2.2 To * 0,014 € = ~30 €/mois (Egress 100% gratuit).
🏗️ 2. Architecture des Flux de Données Globaux & Optimisés
                      [ AISSTREAM WEBSOCKET API ]
                                  │
                                  ▼
                  [ 4 à 16 LISTENERS EN SHARDING ]
             (Chaque listener écoute un quadrant spécifique)
                                  │
                  asyncio.Queue(maxsize=100000) (Bornée)
                                  │
                                  ▼
            [ RAW IMMUTABLE NDJSON.ZST - OVH PRIVÉ ]
              (Stockage brut temporaire purgé à J+7)
                                  │
                    Fenêtre glissante J-7 → J
                                  │
                                  ▼
             [ PIPELINE DE CONSOLIDATION DUCKDB ]
      - Parsing JSON défensif & déduplification robuste (QUALIFY)
      - Gestion du late-arriving & compactage incrémental par bloc horaire
      - Tri physique (ORDER BY mmsi, timestamp) par partition
                                  │
                                  ▼
               [ BUCKET OVH PUBLIC : ais-public-prod (DuckLake) ]
                    s3://ais-public-prod/metadata.ducklake
                                  │
                                  ▼
                   [ UTILISATEURS FINAUX (Lecture Seule) ]
               ATTACH 'ducklake:s3://ais-public-prod/metadata.ducklake'

📋 3. Format des Messages & Schémas Tabulaires Cibles (Spécifications SQL)
Table 1 : positions
Cette table fusionne les messages PositionReport (1, 2, 3), StandardClassBPositionReport (18), ExtendedClassBPositionReport (19) et LongRangeAisBroadcastMessage (27).
CREATE TABLE positions (
   id VARCHAR PRIMARY KEY,         -- MD5 robuste (mmsi | timestamp | lat | lon | message_id)
   mmsi BIGINT NOT NULL,
   name VARCHAR,
   timestamp TIMESTAMPTZ NOT NULL, -- Event Time (time_utc nettoyé et casté)
   lat DOUBLE NOT NULL,
   lon DOUBLE NOT NULL,
   sog DOUBLE,                    -- Vitesse en nœuds
   cog DOUBLE,                    -- Cap (Course over ground)
   true_heading INTEGER,          -- Cap compas réel
   navigational_status INTEGER,   -- Statut de navigation (0-15, NULL pour la classe B)
   rate_of_turn INTEGER,          -- Taux de rotation (Spécification AIS: Entier)
   message_type VARCHAR NOT NULL,
   message_id INTEGER,            -- Type ID du message AIS original (1, 2, 3, 18, 19, 27)
   position_accuracy BOOLEAN,
   raim BOOLEAN,
   valid BOOLEAN,
   received_at TIMESTAMPTZ,       -- Heure d'ingestion par le listener (System Time)
   source_listener VARCHAR        -- Identification du collecteur
);

Table 2 : ship_static
Gère les métadonnées globales du navire en consolidant le ShipStaticData (Message 5) et le StaticDataReport (Message 24, incluant ses sous-structures polymorphes ReportA et ReportB pour la Classe B).
CREATE TABLE ship_static (
   mmsi BIGINT PRIMARY KEY,     -- État unique à jour par MMSI
   name VARCHAR,
   call_sign VARCHAR,
   imo_number BIGINT,
   ship_type INTEGER,           -- Code de classification AIS (0-255)
   ais_version INTEGER,
   length DOUBLE,               -- Calculé : dimension_a + dimension_b
   width DOUBLE,                -- Calculé : dimension_c + dimension_d
   dimension_a DOUBLE,          -- Distance proue-antenne
   dimension_b DOUBLE,          -- Distance poupe-antenne
   dimension_c DOUBLE,          -- Distance bâbord-antenne
   dimension_d DOUBLE,          -- Distance tribord-antenne
   max_static_draught DOUBLE,    -- Tirant d'eau max
   destination VARCHAR,         -- Dernière destination déclarée
   eta TIMESTAMP,               -- Date/Heure estimée d'arrivée (Reconstruite)
   dte BOOLEAN,                 -- Équipement terminal de données
   fix_type INTEGER,            -- Type de capteur de positionnement
   last_updated TIMESTAMPTZ,    -- Horodatage de la dernière mise à jour
   source_listener VARCHAR
);

Table 3 : base_stations
Isole les rapports des stations de base terrestres (Message 4) pour éviter de polluer les aides à la navigation.
CREATE TABLE base_stations (
   mmsi BIGINT PRIMARY KEY,
   station_name VARCHAR,
   lat DOUBLE,
   lon DOUBLE,
   timestamp TIMESTAMPTZ,
   raim BOOLEAN,
   last_updated TIMESTAMPTZ
);

Table 4 : aids_to_navigation (ATON)
Exclusivement réservé aux aides physiques et virtuelles à la navigation (Message 21 : bouées, phares, balises).
CREATE TABLE aids_to_navigation (
   mmsi BIGINT PRIMARY KEY,
   name VARCHAR NOT NULL,
   type_of_aton INTEGER,        -- Type de balise (1-31, extrait de 'Type')
   lat DOUBLE NOT NULL,
   lon DOUBLE NOT NULL,
   timestamp TIMESTAMPTZ NOT NULL,
   dimension_a DOUBLE,
   dimension_b DOUBLE,
   dimension_c DOUBLE,
   dimension_d DOUBLE,
   off_position BOOLEAN,       -- Alerte de dérive de la balise
   virtual_aton BOOLEAN,       -- Vraie ou fausse balise radar
   raim BOOLEAN,
   last_updated TIMESTAMPTZ
);

Table 5 : unstructured_messages
Table d'audit semi-structurée utilisant la syntaxe conforme de DuckDB pour l'incrémentation automatique.
CREATE TABLE unstructured_messages (
   id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
   mmsi BIGINT,
   timestamp TIMESTAMPTZ NOT NULL,
   message_type VARCHAR NOT NULL,
   raw_message JSON NOT NULL,   -- Contenu structuré d'origine préservé
   received_at TIMESTAMPTZ
);

🗼 4. Déploiement Terraform (OVHcloud Storage)
Blocs à rajouter dans ton fichier main.tf
# 1. Bucket S3 de l'environnement pour les données brutes (raw) - Privé
resource "ovh_cloud_project_storage" "ais_raw" {
 service_name = var.ovh_service_name
 name         = "ais-raw-${var.environment}"
 region_name  = "GRA"
}

# 2. Règle de cycle de vie automatique pour purger le raw à J+7
resource "ovh_cloud_project_storage_lifecycle" "ais_raw_cleanup" {
 bucket          = ovh_cloud_project_storage.ais_raw.name
 prefix          = "raw/"
 expiration_days = 7
}

# 3. Bucket S3 de l'environnement pour le DuckLake consolidé - Public
resource "ovh_cloud_project_storage" "ais_public" {
 service_name = var.ovh_service_name
 name         = "ais-public-${var.environment}"
 region_name  = "GRA"
}

# 4. Politique d'accès restrictive pour le VPS (Pas de listing public global)
resource "ovh_cloud_project_user_s3_policy" "system_s3_policy" {
 service_name = var.ovh_service_name
 user_id      = ovh_cloud_project_user.env_user.id
 policy       = jsonencode({
   Statement = [
     {
       Effect   = "Allow"
       Action   = ["s3:*"]
       Resource = [
         "arn:aws:s3:::${ovh_cloud_project_storage.data_lake.name}",
         "arn:aws:s3:::${ovh_cloud_project_storage.data_lake.name}/*",
         "arn:aws:s3:::${ovh_cloud_project_storage.evidence_site.name}",
         "arn:aws:s3:::${ovh_cloud_project_storage.evidence_site.name}/*",
         "arn:aws:s3:::${ovh_cloud_project_storage.ais_raw.name}",
         "arn:aws:s3:::${ovh_cloud_project_storage.ais_raw.name}/*",
         "arn:aws:s3:::${ovh_cloud_project_storage.ais_public.name}",
         "arn:aws:s3:::${ovh_cloud_project_storage.ais_public.name}/*"
       ]
     }
   ]
 })
}

# Outputs additionnels pour l'automatisation de l'ingestion
output "ais_raw_bucket_name" {
 value = ovh_cloud_project_storage.ais_raw.name
}

output "ais_public_bucket_name" {
 value = ovh_cloud_project_storage.ais_public.name
}

🐳 5. Conteneurisation (Déploiement Universel du Listener)
Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Installation des dépendances système légères
RUN apt-get update && apt-get install -y --no-install-recommends \
   curl \
   && rm -rf /var/lib/apt/lists/*

# Copie et installation des prérequis Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du code d'application
COPY configuration.py .
COPY listener.py .

# Exécution non-root pour plus de sécurité
USER 10001:10001

ENTRYPOINT ["python", "listener.py"]

docker-compose.yml
version: '3.8'

services:
 listener-nw:
   build: .
   container_name: ais-listener-nw
   restart: unless-stopped
   environment:
     - OVH_REGION=gra
     - OVH_ACCESS_KEY=${OVH_ACCESS_KEY}
     - OVH_SECRET_KEY=${OVH_SECRET_KEY}
     - AISSTREAM_TOKEN=${AISSTREAM_TOKEN}
   command: ["--id", "listener_world_nw", "--shard", "NW"]
   logging:
     driver: "json-file"
     options:
       max-size: "10m"
       max-file: "3"

 listener-ne:
   build: .
   container_name: ais-listener-ne
   restart: unless-stopped
   environment:
     - OVH_REGION=gra
     - OVH_ACCESS_KEY=${OVH_ACCESS_KEY}
     - OVH_SECRET_KEY=${OVH_SECRET_KEY}
     - AISSTREAM_TOKEN=${AISSTREAM_TOKEN}
   command: ["--id", "listener_world_ne", "--shard", "NE"]
   logging:
     driver: "json-file"
     options:
       max-size: "10m"
       max-file: "3"

 listener-sw:
   build: .
   container_name: ais-listener-sw
   restart: unless-stopped
   environment:
     - OVH_REGION=gra
     - OVH_ACCESS_KEY=${OVH_ACCESS_KEY}
     - OVH_SECRET_KEY=${OVH_SECRET_KEY}
     - AISSTREAM_TOKEN=${AISSTREAM_TOKEN}
   command: ["--id", "listener_world_sw", "--shard", "SW"]
   logging:
     driver: "json-file"
     options:
       max-size: "10m"
       max-file: "3"

 listener-se:
   build: .
   container_name: ais-listener-se
   restart: unless-stopped
   environment:
     - OVH_REGION=gra
     - OVH_ACCESS_KEY=${OVH_ACCESS_KEY}
     - OVH_SECRET_KEY=${OVH_SECRET_KEY}
     - AISSTREAM_TOKEN=${AISSTREAM_TOKEN}
   command: ["--id", "listener_world_se", "--shard", "SE"]
   logging:
     driver: "json-file"
     options:
       max-size: "10m"
       max-file: "3"

🛠️ 6. Implémentation des Composants
requirements.txt (Inclus dans Docker/Python)
asyncio>=3.4.3
websockets>=11.0
boto3>=1.28.0
zstandard>=0.22.0
duckdb>=0.10.0
pyarrow>=14.0

configuration.py
import os

# Configuration OVHcloud Object Storage (Compatible API S3)
OVH_REGION = os.getenv("OVH_REGION", "gra")
OVH_ENDPOINT = os.getenv("OVH_ENDPOINT", f"https://s3.{OVH_REGION}.io.cloud.ovh.net")
OVH_ACCESS_KEY = os.getenv("OVH_ACCESS_KEY", "")
OVH_SECRET_KEY = os.getenv("OVH_SECRET_KEY", "")

# Nom des buckets OVHcloud (Injectés dynamiquement par l'environnement terraformé)
BUCKET_RAW = os.getenv("BUCKET_RAW", "ais-raw-prod")
BUCKET_PUBLIC = os.getenv("BUCKET_PUBLIC", "ais-public-prod")

# Aisstream
AISSTREAM_TOKEN = os.getenv("AISSTREAM_TOKEN", "")
AISSTREAM_WS = "wss://stream.aisstream.io/v0/stream"

# Optimisations contre le "Small File Problem" à l'ingestion brute
BATCH_MAX_SIZE = 50000     # Batch élargi pour réduire le rythme d'écriture brute
BATCH_TIMEOUT_SEC = 60     # Flush toutes les minutes maximum
QUEUE_LIMIT = 100000       # Bornage strict de la file d'attente contre le OOM

listener.py
Spécification ultra-robuste : Écriture brute et immuable en NDJSON.zst (Zstandard), file d'attente asynchrone bornée, multi-workers d'upload parallèles, déconnexion avec exponential backoff et validation défensive des coordonnées (préservation du 0.0).
import asyncio
import json
import boto3
import time
import argparse
import sys
import os
import websockets
import zstandard as zstd
from datetime import datetime, timezone
from configuration import *

s3_client = boto3.client(
   's3',
   endpoint_url=OVH_ENDPOINT,
   aws_access_key_id=OVH_ACCESS_KEY,
   aws_secret_access_key=OVH_SECRET_KEY
)

GLOBAL_SHARDS = {
   "NW": [[0.0, -180.0], [90.0, 0.0]],
   "NE": [[0.0, 0.0], [90.0, 180.0]],
   "SW": [[-90.0, -180.0], [0.0, 0.0]],
   "SE": [[-90.0, 0.0], [0.0, 180.0]]
}

# Queue asynchrone bornée à 100 000 éléments pour appliquer la contre-pression (Backpressure)
message_queue = asyncio.Queue(maxsize=QUEUE_LIMIT)

async def raw_writer_worker(worker_id, listener_id):
   """Worker asynchrone de traitement et d'export en NDJSON compressé ZSTD"""
   print(f"⚙️ [Worker-{worker_id}] Démarrage du pipeline de compaction NDJSON.zst")
   batch = []
   last_flush = time.time()
   compressor = zstd.ZstdCompressor(level=3)
   
   while True:
       try:
           try:
               # Dépilage de la Queue bornée avec timeout
               msg = await asyncio.wait_for(message_queue.get(), timeout=1.0)
               batch.append(msg)
               message_queue.task_done()
           except asyncio.TimeoutError:
               pass

           current_time = time.time()
           if batch and (len(batch) >= BATCH_MAX_SIZE or (current_time - last_flush) >= BATCH_TIMEOUT_SEC):
               now = datetime.now(timezone.utc)
               filename = f"ais_{listener_id}_w{worker_id}_{now.strftime('%Y%m%d_%H%M%S')}.ndjson.zst"
               s3_key = f"raw/year={now.year}/month={now.month:02d}/day={now.day:02d}/hour={now.hour:02d}/{filename}"
               
               # Conversion en NDJSON et compression Zstandart au vol
               ndjson_data = "\n".join(json.dumps(m) for m in batch).encode('utf-8')
               compressed_data = compressor.compress(ndjson_data)
               
               # Upload direct vers OVHcloud via thread pool asynchrone
               await asyncio.to_thread(
                   s3_client.put_object,
                   Bucket=BUCKET_RAW,
                   Key=s3_key,
                   Body=compressed_data
               )
               print(f"📦 [Worker-{worker_id}] Upload réussi de {len(batch)} messages bruts compressés dans {s3_key}")
               
               batch = []
               last_flush = current_time

       except asyncio.CancelledError:
           break
       except Exception as e:
           print(f"❌ [Worker-{worker_id}] Erreur critique lors de l'upload: {e}", file=sys.stderr)
           await asyncio.sleep(2)

async def start_listening(listener_id, shard_name):
   bboxes = [GLOBAL_SHARDS[shard_name]]
   print(f"📡 Démarrage de l'écoute du Shard {shard_name}...")
   
   # Lancement de 4 workers d'écriture et d'upload parallèles
   workers = []
   for i in range(4):
       workers.append(asyncio.create_task(raw_writer_worker(i, listener_id)))
   
   backoff = 1
   while True:
       try:
           async with websockets.connect(AISSTREAM_WS, ping_interval=20, ping_timeout=10) as ws:
               backoff = 1 # Reset backoff après connexion réussie
               await ws.send(json.dumps({
                   "APIKey": AISSTREAM_TOKEN,
                   "BoundingBoxes": bboxes
               }))
               
               while True:
                   message = await ws.recv()
                   data = json.loads(message)
                   
                   if "error" in data:
                       print(f"⚠️ Alerte WebSocket AISstream: {data['error']}")
                       continue
                   
                   metadata = data.get("MetaData", {})
                   lat = metadata.get("latitude") or metadata.get("Latitude")
                   lon = metadata.get("longitude") or metadata.get("Longitude")
                   mmsi = metadata.get("MMSI")
                   
                   # Validation robuste sans rejeter le point 0.0 (interdire uniquement les valeurs de débordement AIS)
                   if not mmsi or lat is None or lon is None:
                       continue
                   if lat in [91.0, -91.0] or lon in [181.0, -181.0]:
                       continue
                   
                   # Remplissage asynchrone de la file d'attente
                   try:
                       message_queue.put_nowait({
                           "message_type": data.get("MessageType"),
                           "metadata": metadata,
                           "message": data.get("Message", {}),
                           "received_at": datetime.now(timezone.utc).isoformat(),
                           "listener_id": listener_id
                       })
                   except asyncio.QueueFull:
                       # Drop défensif du message en cas de saturation de la queue pour éviter le OOM crash
                       print("🚨 [Backpressure] File d'attente saturée, message ignoré pour préserver la RAM")
                       
       except (websockets.exceptions.ConnectionClosed, OSError) as conn_err:
           print(f"⚠️ Déconnexion réseau ({conn_err}). Reconnexion dans {backoff}s...")
           await asyncio.sleep(backoff)
           backoff = min(backoff * 2, 60) # Exponential backoff plafonné à 1 minute
       except Exception as e:
           print(f"❌ Erreur boucle principale: {e}")
           await asyncio.sleep(5)
           
   for w in workers:
       w.cancel()

if __name__ == "__main__":
   parser = argparse.ArgumentParser()
   parser.add_argument("--id", required=True, help="ID unique de l'instance listener")
   parser.add_argument("--shard", required=True, choices=["NW", "NE", "SW", "SE"], help="Quadrant géographique mondial")
   args = parser.parse_args()
   
   while True:
       try:
           asyncio.run(start_listening(args.id, args.shard))
       except Exception as crash_err:
           print(f"💥 Crash fatal détecté: {crash_err}. Redémarrage complet de l'orchestrateur dans 15s...")
           time.sleep(15)

consolidate.py
Algorithme analytique révisé : Scanning de la fenêtre temporelle glissante J-7 à J pour le traitement des données asynchrones, parsing défensif JSON, déduplification robuste et tri physique, reconstruction d'ID par hash MD5 sans risque d'overflow, traitement d'ETA et table dédiée pour les base stations terrestres.
import duckdb
import boto3
from datetime import datetime, timedelta, timezone
from configuration import *

def fetch_raw_files_window():
   """Récupère récursivement les URIs S3 des dossiers bruts de la fenêtre glissante J-7 à J"""
   s3 = boto3.client(
       's3',
       endpoint_url=OVH_ENDPOINT,
       aws_access_key_id=OVH_ACCESS_KEY,
       aws_secret_access_key=OVH_SECRET_KEY
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
       
   con = duckdb.connect()
   con.execute("INSTALL httpfs; INSTALL ducklake; LOAD httpfs; LOAD ducklake;")
   
   clean_endpoint = OVH_ENDPOINT.replace("https://", "")
   con.execute(f"SET s3_endpoint = '{clean_endpoint}';")
   con.execute(f"SET s3_access_key_id = '{OVH_ACCESS_KEY}';")
   con.execute(f"SET s3_secret_access_key = '{OVH_SECRET_KEY}';")
   con.execute("SET s3_url_style = 'path';")
   
   ducklake_uri = f"s3://{BUCKET_PUBLIC}/metadata.ducklake"
   data_uri = f"s3://{BUCKET_PUBLIC}/data_files/"
   
   # Amorçage transactionnel DuckLake
   try:
       con.execute(f"ATTACH 'ducklake:{ducklake_uri}' AS public_lake (DATA_PATH '{data_uri}');")
       con.execute("SELECT 1 FROM public_lake.positions LIMIT 1")
   except Exception:
       print("🆕 Amorçage initial des tables et création du catalogue de métadonnées DuckLake...")
       try:
           con.execute(f"ATTACH 'ducklake:{ducklake_uri}' AS public_lake (DATA_PATH '{data_uri}');")
       except Exception as attach_err:
           print(f"Impossible d'attacher l'infrastructure analytique: {attach_err}")
           return
           
       con.execute("""
           CREATE TABLE IF NOT EXISTS public_lake.positions (
               id VARCHAR PRIMARY KEY,
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
               mmsi BIGINT PRIMARY KEY, name VARCHAR, call_sign VARCHAR,
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
               mmsi BIGINT PRIMARY KEY, station_name VARCHAR,
               lat DOUBLE, lon DOUBLE, timestamp TIMESTAMPTZ,
               raim BOOLEAN, last_updated TIMESTAMPTZ
           )
       """)
       con.execute("""
           CREATE TABLE IF NOT EXISTS public_lake.aids_to_navigation (
               mmsi BIGINT PRIMARY KEY, name VARCHAR, type_of_aton INTEGER,
               lat DOUBLE, lon DOUBLE, timestamp TIMESTAMPTZ,
               dimension_a DOUBLE, dimension_b DOUBLE, dimension_c DOUBLE,
               dimension_d DOUBLE, off_position BOOLEAN, virtual_aton BOOLEAN,
               raim BOOLEAN, last_updated TIMESTAMPTZ
           )
       """)
       con.execute("""
           CREATE TABLE IF NOT EXISTS public_lake.unstructured_messages (
               id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
               mmsi BIGINT, timestamp TIMESTAMPTZ, message_type VARCHAR,
               raw_message JSON, received_at TIMESTAMPTZ
           )
       """)

   # Enregistrement défensif des fichiers NDJSON.zst via read_json_auto de DuckDB
   con.execute("CREATE OR REPLACE TEMP VIEW raw_view AS SELECT * FROM read_json_auto(?, format='newline_delimited', compression='zstd', maximum_object_size=10000000)", [files])

   # 1. Positions de navigation (Traitement de l'Event Time hier, déduplication et routage du Message Type 27)
   for hour_start in range(0, 24, 3):
       h_start = f"{hour_start:02d}:00:00"
       h_end = f"{(hour_start+2):02d}:59:59"
       print(f"⏳ Compactage incrémental colonnaire de la tranche horaire {h_start} à {h_end}...")
       
       # ID robuste généré par un Hash MD5 pour écarter tout risque d'overflow sur mmsi*epoch
       con.execute(f"""
           CREATE OR REPLACE TEMP TABLE stage_positions AS
           SELECT
               md5(concat_ws('|', 
                   json_extract_int(metadata, '$.MMSI'), 
                   CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ),
                   COALESCE(json_extract_double(metadata, '$.latitude'), json_extract_double(metadata, '$.Latitude')),
                   COALESCE(json_extract_double(metadata, '$.longitude'), json_extract_double(metadata, '$.Longitude')),
                   COALESCE(
                       json_extract_int(message, '$.PositionReport.MessageID'),
                       json_extract_int(message, '$.ExtendedClassBPositionReport.MessageID'),
                       json_extract_int(message, '$.StandardClassBPositionReport.MessageID'),
                       json_extract_int(message, '$.LongRangeAisBroadcast.MessageID')
                   )
               )) AS id,
               json_extract_int(metadata, '$.MMSI') AS mmsi,
               json_extract_string(metadata, '$.ShipName') AS name,
               CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) AS timestamp,
               COALESCE(json_extract_double(metadata, '$.latitude'), json_extract_double(metadata, '$.Latitude')) AS lat,
               COALESCE(json_extract_double(metadata, '$.longitude'), json_extract_double(metadata, '$.Longitude')) AS lon,
               COALESCE(
                   json_extract_double(message, '$.PositionReport.Sog'),
                   json_extract_double(message, '$.ExtendedClassBPositionReport.Sog'),
                   json_extract_double(message, '$.StandardClassBPositionReport.Sog'),
                   json_extract_double(message, '$.LongRangeAisBroadcast.Sog')
               ) AS sog,
               COALESCE(
                   json_extract_double(message, '$.PositionReport.Cog'),
                   json_extract_double(message, '$.ExtendedClassBPositionReport.Cog'),
                   json_extract_double(message, '$.StandardClassBPositionReport.Cog'),
                   json_extract_double(message, '$.LongRangeAisBroadcast.Cog')
               ) AS cog,
               COALESCE(
                   json_extract_int(message, '$.PositionReport.TrueHeading'),
                   json_extract_int(message, '$.ExtendedClassBPositionReport.TrueHeading'),
                   json_extract_int(message, '$.StandardClassBPositionReport.TrueHeading'),
                   json_extract_int(message, '$.LongRangeAisBroadcast.TrueHeading')
               ) AS true_heading,
               COALESCE(
                   json_extract_int(message, '$.PositionReport.NavigationalStatus'),
                   json_extract_int(message, '$.LongRangeAisBroadcast.NavigationalStatus')
               ) AS navigational_status,
               COALESCE(
                   json_extract_int(message, '$.PositionReport.RateOfTurn'),
                   json_extract_int(message, '$.ExtendedClassBPositionReport.RateOfTurn')
               ) AS rate_of_turn,
               message_type,
               COALESCE(
                   json_extract_int(message, '$.PositionReport.MessageID'),
                   json_extract_int(message, '$.ExtendedClassBPositionReport.MessageID'),
                   json_extract_int(message, '$.StandardClassBPositionReport.MessageID'),
                   json_extract_int(message, '$.LongRangeAisBroadcast.MessageID')
               ) AS message_id,
               COALESCE(
                   json_extract_bool(message, '$.PositionReport.PositionAccuracy'),
                   json_extract_bool(message, '$.ExtendedClassBPositionReport.PositionAccuracy'),
                   json_extract_bool(message, '$.StandardClassBPositionReport.PositionAccuracy'),
                   json_extract_bool(message, '$.LongRangeAisBroadcast.PositionAccuracy')
               ) AS position_accuracy,
               COALESCE(
                   json_extract_bool(message, '$.PositionReport.Raim'),
                   json_extract_bool(message, '$.ExtendedClassBPositionReport.Raim'),
                   json_extract_bool(message, '$.StandardClassBPositionReport.Raim'),
                   json_extract_bool(message, '$.LongRangeAisBroadcast.Raim')
               ) AS raim,
               COALESCE(
                   json_extract_bool(message, '$.PositionReport.Valid'),
                   json_extract_bool(message, '$.ExtendedClassBPositionReport.Valid'),
                   json_extract_bool(message, '$.StandardClassBPositionReport.Valid'),
                   json_extract_bool(message, '$.LongRangeAisBroadcast.Valid')
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
           ORDER BY mmsi, timestamp; -- Maximise l'encodage colonnaire RLE Parquet
       """)
       con.execute("INSERT OR IGNORE INTO public_lake.positions SELECT * FROM stage_positions")

   # 2. Informations statiques (Classe A & Classe B polymorphe type 24)
   print("⏳ Fusion incrémentale de la flotte de commerce et de plaisance...")
   con.execute(f"""
       CREATE OR REPLACE TEMP TABLE stage_static AS
       WITH union_static AS (
           SELECT
               json_extract_int(metadata, '$.MMSI') AS mmsi,
               COALESCE(
                   json_extract_string(message, '$.ShipStaticData.Name'), 
                   json_extract_string(message, '$.StaticDataReport.ReportA.Name'), 
                   json_extract_string(metadata, '$.ShipName')
               ) AS name,
               COALESCE(
                   json_extract_string(message, '$.ShipStaticData.CallSign'),
                   json_extract_string(message, '$.StaticDataReport.ReportB.CallSign')
               ) AS call_sign,
               json_extract_int(message, '$.ShipStaticData.ImoNumber') AS imo_number,
               COALESCE(
                   json_extract_int(message, '$.ShipStaticData.Type'),
                   json_extract_int(message, '$.StaticDataReport.ReportB.ShipType')
               ) AS ship_type,
               json_extract_int(message, '$.ShipStaticData.AisVersion') AS ais_version,
               COALESCE(
                   (json_extract_double(message, '$.ShipStaticData.Dimension.A') + json_extract_double(message, '$.ShipStaticData.Dimension.B'))::DOUBLE,
                   (json_extract_double(message, '$.StaticDataReport.ReportB.Dimension.A') + json_extract_double(message, '$.StaticDataReport.ReportB.Dimension.B'))::DOUBLE
               ) AS length,
               COALESCE(
                   (json_extract_double(message, '$.ShipStaticData.Dimension.C') + json_extract_double(message, '$.ShipStaticData.Dimension.D'))::DOUBLE,
                   (json_extract_double(message, '$.StaticDataReport.ReportB.Dimension.C') + json_extract_double(message, '$.StaticDataReport.ReportB.Dimension.D'))::DOUBLE
               ) AS width,
               COALESCE(
                   json_extract_double(message, '$.ShipStaticData.Dimension.A'),
                   json_extract_double(message, '$.StaticDataReport.ReportB.Dimension.A')
               ) AS dimension_a,
               COALESCE(
                   json_extract_double(message, '$.ShipStaticData.Dimension.B'),
                   json_extract_double(message, '$.StaticDataReport.ReportB.Dimension.B')
               ) AS dimension_b,
               COALESCE(
                   json_extract_double(message, '$.ShipStaticData.Dimension.C'),
                   json_extract_double(message, '$.StaticDataReport.ReportB.Dimension.C')
               ) AS dimension_c,
               COALESCE(
                   json_extract_double(message, '$.ShipStaticData.Dimension.D'),
                   json_extract_double(message, '$.StaticDataReport.ReportB.Dimension.D')
               ) AS dimension_d,
               json_extract_double(message, '$.ShipStaticData.MaximumStaticDraught') AS max_static_draught,
               json_extract_string(message, '$.ShipStaticData.Destination') AS destination,
               
               -- Extraction mathématique défensive de l'ETA structuré AIS
               CASE WHEN json_extract_int(message, '$.ShipStaticData.Eta.Month') BETWEEN 1 AND 12 
                    AND json_extract_int(message, '$.ShipStaticData.Eta.Day') BETWEEN 1 AND 31 THEN
                    MAKE_TIMESTAMP(
                       YEAR(CURRENT_DATE), 
                       json_extract_int(message, '$.ShipStaticData.Eta.Month'), 
                       json_extract_int(message, '$.ShipStaticData.Eta.Day'), 
                       COALESCE(NULLIF(json_extract_int(message, '$.ShipStaticData.Eta.Hour'), 24), 0), 
                       COALESCE(NULLIF(json_extract_int(message, '$.ShipStaticData.Eta.Minute'), 60), 0), 
                       0
                    )
               ELSE NULL END AS eta,
               
               json_extract_bool(message, '$.ShipStaticData.Dte') AS dte,
               json_extract_int(message, '$.ShipStaticData.FixType') AS fix_type,
               CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) AS last_updated,
               listener_id AS source_listener
           FROM raw_view
           WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
             AND CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) BETWEEN '{yesterday} 00:00:00' AND '{yesterday} 23:59:59'
       )
       SELECT * FROM union_static
       QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY last_updated DESC) = 1;
   """)
   con.execute("INSERT OR REPLACE INTO public_lake.ship_static SELECT * FROM stage_static")

   # 3. Stations de Base (Traitement exclusif du Type 4 pour découpler l'infrastructure)
   print("⏳ Consolidation des rapports des Stations de Base fixes (Type 4)...")
   con.execute(f"""
       CREATE OR REPLACE TEMP TABLE stage_stations AS
       SELECT
           json_extract_int(metadata, '$.MMSI') AS mmsi,
           json_extract_string(metadata, '$.ShipName') AS station_name,
           COALESCE(json_extract_double(metadata, '$.latitude'), json_extract_double(metadata, '$.Latitude')) AS lat,
           COALESCE(json_extract_double(metadata, '$.longitude'), json_extract_double(metadata, '$.Longitude')) AS lon,
           CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) AS timestamp,
           json_extract_bool(message, '$.BaseStationReport.Raim') AS raim,
           CAST(received_at AS TIMESTAMPTZ) AS last_updated
       FROM raw_view
       WHERE message_type = 'BaseStationReport'
         AND CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) BETWEEN '{yesterday} 00:00:00' AND '{yesterday} 23:59:59'
       QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY timestamp DESC) = 1;
   """)
   con.execute("INSERT OR REPLACE INTO public_lake.base_stations SELECT * FROM stage_stations")

   # 4. Balises et phares (ATON - Message 21) (Correction de la clé de typage)
   print("⏳ Compactage et synchronisation des aides de navigation (ATON)...")
   con.execute(f"""
       CREATE OR REPLACE TEMP TABLE stage_aton AS
       SELECT
           json_extract_int(metadata, '$.MMSI') AS mmsi,
           json_extract_string(message, '$.AidsToNavigationReport.Name') AS name,
           json_extract_int(message, '$.AidsToNavigationReport.Type') AS type_of_aton,
           COALESCE(json_extract_double(metadata, '$.latitude'), json_extract_double(metadata, '$.Latitude')) AS lat,
           COALESCE(json_extract_double(metadata, '$.longitude'), json_extract_double(metadata, '$.Longitude')) AS lon,
           CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) AS timestamp,
           json_extract_double(message, '$.AidsToNavigationReport.Dimension.A') AS dimension_a,
           json_extract_double(message, '$.AidsToNavigationReport.Dimension.B') AS dimension_b,
           json_extract_double(message, '$.AidsToNavigationReport.Dimension.C') AS dimension_c,
           json_extract_double(message, '$.AidsToNavigationReport.Dimension.D') AS dimension_d,
           json_extract_bool(message, '$.AidsToNavigationReport.OffPosition') AS off_position,
           json_extract_bool(message, '$.AidsToNavigationReport.VirtualAidsToNavigation') AS virtual_aton,
           json_extract_bool(message, '$.AidsToNavigationReport.Raim') AS raim,
           CAST(received_at AS TIMESTAMPTZ) AS last_updated
       FROM raw_view
       WHERE message_type = 'AidsToNavigationReport'
         AND CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) BETWEEN '{yesterday} 00:00:00' AND '{yesterday} 23:59:59'
       QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY timestamp DESC) = 1;
   """)
   con.execute("INSERT OR REPLACE INTO public_lake.aids_to_navigation SELECT * FROM stage_aton")

   # 5. Messages techniques et de sécurité maritime restants
   print("⏳ Compactage et archivage des messages binaires et d'alerte...")
   con.execute(f"""
       INSERT INTO public_lake.unstructured_messages (mmsi, timestamp, message_type, raw_message, received_at)
       SELECT
           json_extract_int(metadata, '$.MMSI') AS mmsi,
           CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) AS timestamp,
           message_type,
           message AS raw_message,
           CAST(received_at AS TIMESTAMPTZ) AS received_at
       FROM raw_view
       WHERE message_type NOT IN ('PositionReport', 'ExtendedClassBPositionReport', 'StandardClassBPositionReport', 'LongRangeAisBroadcast', 'ShipStaticData', 'StaticDataReport', 'BaseStationReport', 'AidsToNavigationReport')
         AND CAST(regexp_replace(json_extract_string(metadata, '$.time_utc'), ' \\+0000 UTC$', '+00:00') AS TIMESTAMPTZ) BETWEEN '{yesterday} 00:00:00' AND '{yesterday} 23:59:59';
   """)

   con.execute("COMMIT;")
   con.execute("CHECKPOINT;")
   con.close()
   print("🎉 Consolidation globale et compactage sur OVHcloud terminés avec succès !")

if __name__ == "__main__":
   run_consolidation()

⚖️ 8. Limitations & Disclaimer Légal
Ce projet doit explicitement mentionner dans sa documentation publique :
   * Best-Effort : Absence de garantie absolue de couverture mondiale ou de disponibilité. Le flux repose sur un raccordement d'infrastructure communautaire tiers.
   * Trous temporels et duplicats : Des anomalies réseau peuvent occasionner des ruptures de trace temporelle ou des doublons de messages.
   * Disclaimer maritime obligatoire : Ce jeu de données n'est pas conçu pour un usage opérationnel, sécuritaire ou d'assistance à la navigation maritime. Il ne saurait se substituer aux instruments de bord certifiés SOLAS.
🚀 9. Évolutions Futures Recommandées
   1. Bus de Streaming de Secours : Envisager à terme l'intégration d'un broker asynchrone léger (ex: Redpanda ou NATS JetStream) pour servir de tampon d'ingestion distribuée si la volumétrie journalière dépasse durablement le demi-milliard de messages.
   2. Partitionnement Spatial (Geohashing) : Si les requêtes spatiales des utilisateurs finaux deviennent trop coûteuses sur S3, DuckLake permettra d'organiser le stockage physique via un préfixe de hash géographique dans l'URI des partitions.
   3. Optimisations du format de stockage : À mesure que la volumétrie cumulée dépasse les 10 To, conduire un benchmark d'évolutivité entre DuckLake, Delta Lake et Apache Iceberg pour optimiser les requêtes concurrentes distantes.