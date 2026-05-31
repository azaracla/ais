import os
from dotenv import load_dotenv

load_dotenv()  # Charger les variables d'environnement depuis le fichier .env

# Configuration OVHcloud Object Storage (Compatible API S3)
OVH_REGION = os.getenv("OVH_REGION", "gra")
OVH_ENDPOINT = os.getenv("OVH_ENDPOINT", "https://s3.gra.io.cloud.ovh.net")
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
