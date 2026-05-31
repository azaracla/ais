# DuckLake Public AIS

C'est un test technique : un système de reporting sans serveur basé sur DuckLake (Parquet sur S3, accès en lecture seule) et DuckDB WASM dans le navigateur.

Le principe : stocker les données une fois, optimisées côté stockage (partitionnement, row groups, compression), et laisser le client télécharger uniquement les bytes nécessaires via des requêtes HTTP Range. Résultat : des requêtes rapides sur des millions de lignes sans backend, sans base dédiée, sans presque rien transférer sur le réseau.

---

## Stack

- **Stockage** : OVHcloud S3 (path-style), bucket public en lecture seule
- **Moteur côté client** : DuckDB WASM, qui tourne dans le navigateur et attache le DuckLake distant en lecture seule
- **Transport** : HTTP Range Requests — DuckDB WASM ne télécharge que les row groups et colonnes utiles pour la requête, rien de plus
- **Optimisations stockage** : les fichiers Parquet sont partitionnés par date, triés par timestamp/MMSI, organisés en row groups de 100k lignes, et compressés

---

## Architecture du pipeline

**Ingestion** (`pipeline/listener.py`)
Écoute le WebSocket AISstream par quadrant géographique (NW, NE, SW, SE). Lots de 50 000 messages ou 60 secondes, compressés en NDJSON.zst, uploadés sur S3 dans `raw/year=YYYY/month=MM/day=DD/hour=HH/`.

**Consolidation** (`pipeline/consolidate_optimized.py`)
Télécharge les bruts d'une plage de dates, parse en parallèle, déduplique via DuckDB (ROW_NUMBER), et écrit un Parquet unifié par jour dans `silver/` (53 champs unifiés).

**Publication DuckLake** (`pipeline/publish_ducklake.py`)
Convertit le silver en 6 tables gold : `messages`, `vessels_positions`, `vessel_tracks`, `base_stations`, `aids_to_navigation`, `vessels`. Les Parquets sont uploadés vers le bucket public et enregistrés dans le catalogue DuckLake (`ais.ducklake`). Le catalogue pointe vers les fichiers sur S3 — le client les résout au moment de la requête.

---

## Application web

**Frontend** (`front/`)
React 19 + TypeScript + Maplibre GL. Le navigateur initialise DuckDB WASM, attache le DuckLake public via `ATTACH 'https://.../ais.ducklake'`, puis exécute des SQL directement sur les fichiers Parquet distants. Les range requests ne ramènent que les données nécessaires : par exemple, une requête de positions récentes dans un rectangle ne télécharge que les row groups de la bonne date, les colonnes lat/lon/type/mmsi, et les lignes filtrées.

Fonctionnalités : positions en temps réel ou historiques par date/heure, filtre par type de navire, détail et trajectoire au clic, sélection par rectangle, imagerie satellite.

**Proxy satellite** (`front/services/satellite-proxy/`)
FastAPI qui proxyie Google Earth Engine pour récupérer les tuiles Sentinel-1 et Sentinel-2.

---

## Optimisations clés

**Côté stockage** : les fichiers gold sont partitionnés par date, triés, et organisés en row groups de 100k lignes. DuckDB peut ainsi skipper les partitions et row groups qui ne correspondent pas au filtre de la requête sans les télécharger.

**Côté client** : DuckDB WASM ouvre les fichiers Parquet via des range requests HTTP. Il ne télécharge que les métadonnées du fichier (quelques Ko), puis les row groups et colonnes strictement nécessaires. Une requête de positions sur une zone et une heure précises peut ne transférer que quelques centaines de Ko au lieu du fichier entier.

**Catalogue DuckLake** : un fichier JSON qui liste tous les fichiers Parquet et leur partitionnement. Le client le télécharge une fois, puis résout chaque requête contre le bon fichier.

---

## Déploiement

```bash
# Pipeline
pip install -r requirements.txt
docker-compose up -d
python pipeline/consolidate_optimized.py --date YYYY-MM-DD
python pipeline/publish_ducklake.py --date YYYY-MM-DD

# Frontend
cd front && npm install && npm run dev

# Proxy satellite
cd front/services/satellite-proxy && uvicorn main:app --port 8000

# Infrastructure
cd infra && terraform init && terraform apply
```

---

## Structure

```
pipeline/         ingestion, consolidation, publication DuckLake
front/            application React + DuckDB WASM + proxy satellite
infra/            Terraform OVHcloud
scripts/          utilitaires
```

---

## Limitations

Best-effort, pas de garantie de couverture mondiale complète. Ce projet est un test technique, pas un service de sécurité maritime.
