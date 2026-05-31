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

from botocore.client import Config

s3_client = boto3.client(
   's3',
   endpoint_url=OVH_ENDPOINT,
   aws_access_key_id=OVH_ACCESS_KEY,
   aws_secret_access_key=OVH_SECRET_KEY,
   config=Config(s3={'addressing_style': 'path'})
)

GLOBAL_SHARDS = {
   "NW": [[0.0, -180.0], [90.0, 0.0]],
   "NE": [[0.0, 0.0], [90.0, 180.0]],
   "SW": [[-90.0, -180.0], [0.0, 0.0]],
   "SE": [[-90.0, 0.0], [0.0, 180.0]],
   "WORLD": [[-90.0, -180.0], [90.0, 180.0]]
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
                   
                   # Heartbeat log
                   if not hasattr(start_listening, "msg_count"):
                       start_listening.msg_count = 0
                   start_listening.msg_count += 1
                   if start_listening.msg_count % 1000 == 0:
                       print(f"💓 [Listener] {start_listening.msg_count} messages reçus au total...")
                   
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
   parser.add_argument("--shard", required=True, choices=["NW", "NE", "SW", "SE", "WORLD"], help="Quadrant géographique mondial")
   args = parser.parse_args()
   
   while True:
       try:
           asyncio.run(start_listening(args.id, args.shard))
       except Exception as crash_err:
           print(f"💥 Crash fatal détecté: {crash_err}. Redémarrage complet de l'orchestrateur dans 15s...")
           time.sleep(15)
