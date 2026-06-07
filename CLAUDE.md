# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Serverless AIS maritime traffic viewer. Ingest raw AIS messages via WebSocket → consolidate into Parquet → publish as a DuckLake catalog on public S3. The browser runs DuckDB WASM, attaches the remote DuckLake catalog, and queries Parquet files directly via HTTP Range requests — no backend at query time.

## Stack

- **Storage**: OVHcloud S3 (path-style addressing), public-read bucket
- **Pipeline**: Python 3.13 + uv (asyncio, boto3, DuckDB, PyArrow, zstandard)
- **Frontend**: React 19 + TypeScript + Maplibre GL + `@duckdb/duckdb-wasm`
- **Satellite proxy**: FastAPI (Python) proxying Google Earth Engine tiles
- **Infra**: Terraform (OVH provider)

## Common Commands

```bash
# Pipeline (uv — package manager, no venv activation needed)
uv sync                                         # install all deps (first time)
uv run python pipeline/v3/pipeline.py --date YYYY-MM-DD
uv run python pipeline/v3/pipeline.py --from YYYY-MM-DD --to YYYY-MM-DD
uv run python pipeline/v3/pipeline.py --date YYYY-MM-DD --force
uv run python pipeline/v3/pipeline.py --date YYYY-MM-DD --tables vessels_positions,vessels

# Legacy v2 pipeline
docker-compose up -d                            # start listener(s)
python pipeline/consolidate_optimized.py --date YYYY-MM-DD
python pipeline/publish_ducklake.py --date YYYY-MM-DD

# Frontend
cd front && npm install && npm run dev          # Vite dev server
cd front && npm run build                       # production build
cd front && npm run lint                        # ESLint

# Satellite proxy
cd front/services/satellite-proxy && uv run uvicorn main:app --port 8000

# Infrastructure
cd infra && terraform init && terraform apply

# Test S3 connectivity
python scripts/test_s3.py
```

## Architecture

### Data Pipeline (3 stages)

1. **Ingestion** (`pipeline/listener.py`): Connects to AISstream WebSocket, shards by geographic quadrant (NW/NE/SW/SE). Batches 50k messages or 60s intervals → compresses as NDJSON.zst → uploads to `s3://ais-raw-prod/raw/year=YYYY/month=MM/day=DD/hour=HH/`. Uses `asyncio.Queue(maxsize=100000)` for backpressure. Dockerized via `docker-compose.yml`.

2. **Consolidation** (`pipeline/consolidate_optimized.py`): Reads all raw files for a date, parses in parallel via `ProcessPoolExecutor`, writes temp Parquet files, then runs a DuckDB streaming query to deduplicate (`QUALIFY ROW_NUMBER() OVER ...`) and produce a single sorted Parquet (`messages_consolidated.parquet`) in `silver/`. Sets `memory_limit='10GB'` and spills to disk.

3. **Publication** (`pipeline/publish_ducklake.py`): Reads the consolidated silver Parquet, derives 6 gold tables from it:
   - `messages` — full 53-column schema (partitioned by year/month/day)
   - `vessels_positions` — position messages only (partitioned by year/month/day)
   - `vessel_tracks` — downsampled positions with integer coords (partitioned by date, 10-minute bucket)
   - `base_stations` — from BaseStationReport messages
   - `aids_to_navigation` — from AidsToNavigationReport messages
   - `vessels` — latest static vessel info, deduplicated across all silver

   Uploads all gold Parquets to the public bucket, registers them in the DuckLake catalog (`ais.ducklake`), uploads catalog with `public-read` ACL.

**Config** (`pipeline/configuration.py`): Loads from `.env` via `python-dotenv`. All env vars: `OVH_REGION`, `OVH_ENDPOINT`, `OVH_ACCESS_KEY`, `OVH_SECRET_KEY`, `BUCKET_RAW`, `BUCKET_PUBLIC`, `AISSTREAM_TOKEN`.

**CI** (`.github/workflows/ducklake-hourly.yml`): Runs at `30 * * * *` on GitHub Actions. Installs DuckDB CLI, processes the previous hour. References `pipeline/v2/` scripts (different from the `pipeline/` scripts in the repo — represents an in-progress v2).

### Frontend (`front/`)

- **`src/duckdb.ts`**: Singleton DuckDB WASM initialization. Configures HTTP filesystem with `allowFullHTTPReads: false` and `reliableHeadRequests: true` (OVH S3 supports proper HEAD+Range). Attaches the remote DuckLake catalog via `ATTACH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/v2/ais.ducklake'`. Exports `queryLastPositions()` (DISTINCT ON mmsi with spatial+time filters on `vessels_positions` + JOIN `vessels`) and `queryVesselHistory()` (reads `vessel_tracks` with integer coords, up to 3 days back). Streaming queries via `conn.send()`.

- **`src/useVessels.ts`**: React hook managing DuckDB init, debounced viewport-based queries, and accumulated results. Expands bounds by √3 factor to preload data outside viewport.

- **`src/useDraw.ts`**: Rectangle draw tool on Maplibre map. Two-corner click to define bounding box, used for satellite imagery area selection.

- **`src/useSatellite.ts`**: Fetches available satellite dates, tile URLs, acquisition times, and scene footprints from the satellite proxy. Tile URL includes `{z}/{x}/{y}` template for Maplibre raster source.

- **`src/App.tsx`**: Main map component. Renders vessel positions as Maplibre symbol layer (triangle icons colored by ship type). Click handler shows popup with vessel details and triggers async trajectory query. Supports satellite overlay as raster tile layer, scene footprint polygons, and search radius visualization (computed from vessel speed × time since last satellite acquisition).

- **`src/types.ts`**: `Vessel`, `Bounds`, `ShipType`, `Sensor` types and `shipTypeAISToCategory()` mapping AIS type codes to categories (cargo/tanker/passenger/fishing/pleasure).

- **`src/mockData.ts`**: Generates mock vessels in European maritime hot zones. Exports `vesselsToGeoJSON()` for the map layer.

### Satellite Proxy (`front/services/satellite-proxy/`)

FastAPI server proxying Google Earth Engine. Endpoints: `/map` (returns tile URL format for a sensor+date+bbox), `/tiles/{z}/{x}/{y}` (proxies individual tiles), `/scenes` (returns GeoJSON footprints of satellite scenes), `/acquisition-time`, `/available-dates`. Uses `cachetools.TTLCache` (3h TTL) with thread locks for map IDs and tiles. GEE project: `aal-sentinel`.

### Infrastructure (`infra/`)

Terraform for OVHcloud: creates an S3 user with credentials, two buckets (`ais-raw-{env}` and `ais-public-{env}`), S3 policy granting full access, and a null_resource to set public-read ACL on existing objects.

## DuckLake Catalog — Remote Attach

### Read-only (frontend / analytics)

Le catalog est en lecture publique via HTTPS. Pas besoin de credentials S3.

```sql
-- DuckDB WASM / CLI read-only
INSTALL httpfs; LOAD httpfs;
INSTALL ducklake; LOAD ducklake;
SET enable_http_metadata_cache=false;  -- évite HTTP 416 sur fichiers ré-uploadés
ATTACH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake' AS ais
  (TYPE ducklake, DATA_PATH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake.files/',
   OVERRIDE_DATA_PATH true);
```

**Important** :
- Toujours `OVERRIDE_DATA_PATH true` — le catalog stocke le DATA_PATH d'origine (peut être `s3://`), le client lit en HTTPS public.
- `SET enable_http_metadata_cache=false` si le catalog est mis à jour fréquemment, sinon HTTP 416 sur les fichiers ré-uploadés avec un footer différent.
- Le DATA_PATH doit pointer vers le dossier parent des fichiers Parquet (Hive partitioning).
- `ais.ducklake` = metadata catalog, `ais.ducklake.files/` = dossier contenant les `.parquet`.

### Read-write (pipeline Python)

```python
import duckdb
con = duckdb.connect()
con.execute('INSTALL httpfs; INSTALL ducklake; LOAD httpfs; LOAD ducklake')
# S3 credentials obligatoires pour l'écriture
con.execute(f"SET s3_endpoint='{OVH_ENDPOINT.replace('https://', '')}'")
con.execute("SET s3_region='gra'")
con.execute(f"SET s3_access_key_id='{OVH_ACCESS_KEY}'")
con.execute(f"SET s3_secret_access_key='{OVH_SECRET_KEY}'")
con.execute("SET s3_url_style='path'; SET s3_use_ssl=true")

# Attach avec DATA_PATH S3 pour pouvoir écrire
s3_data_path = f"s3://{BUCKET_PUBLIC}/v3/ais.ducklake.files/"
con.execute(f"""
    ATTACH 's3://{BUCKET_PUBLIC}/v3/ais.ducklake' AS ais_lake (
        TYPE ducklake, DATA_PATH '{s3_data_path}',
        OVERRIDE_DATA_PATH true
    )
""")
```

**Pièges** :
- Le DATA_PATH du catalog est **stocké dans le fichier** `ais.ducklake`. Si le pipeline écrit avec `s3://` et le frontend lit en `https://`, le catalog doit avoir `https://` comme DATA_PATH. Le `pipeline.py` utilise `OVERRIDE_DATA_PATH true` à l'attach pour forcer `s3://` en écriture, mais le DATA_PATH stocké reste celui du catalog.
- Pour que les clients HTTPS fonctionnent, le `pipeline.py` stocke `https_data_path` dans le catalog initial (`is_new`). Les updates suivants utilisent `s3_data_path` en session mais préservent le `https_data_path` stocké.
- `AUTOMATIC_MIGRATION true` seulement au premier attach — permet à DuckLake d'ajouter les colonnes manquantes dans le schéma du catalog.

### Nettoyage complet du catalog v3 distant

Supprimer et recréer tout le catalog + données :

```bash
# 1. Supprimer les fichiers Parquet distants
aws s3 rm s3://ais-public-prod/v3/ais.ducklake.files/ --recursive \
  --endpoint-url=https://s3.gra.io.cloud.ovh.net

# 2. Supprimer le catalog
aws s3 rm s3://ais-public-prod/v3/ais.ducklake \
  --endpoint-url=https://s3.gra.io.cloud.ovh.net

# 3. Supprimer le cache local
rm -f pipeline/v3/catalog/ais.ducklake
rm -rf pipeline/v3/output/*
rm -rf pipeline/v3/work/*

# 4. Tout reprocesser (12 jours)
python pipeline/v3/pipeline.py --from 2026-05-26 --to 2026-06-06
```

## Key Conventions

- S3 addressing is **path-style** (`s3.gra.io.cloud.ovh.net/{bucket}/{key}`), not virtual-hosted. Both Python boto3 and DuckDB WASM configs must reflect this.
- Parquet files use **ZSTD compression**, row groups of 100k rows, sorted by `message_type, mmsi, ts`.
- The `vessel_tracks` table stores lat/lon as **integers** (`ROUND(lat * 1e5)`) to reduce Parquet size. Query code divides by `1e5` when reading.
- `consolidate_optimized.py` uses the raw bucket (`BUCKET_RAW`) for both input and output; the output key in `BUCKET_SILVER = BUCKET_RAW` is deliberate — silver files live alongside raw under a `silver/` prefix in the same bucket.
- The frontend DuckLake catalog is at `/v3/` (production). The catalog file is `ais.ducklake`, data files under `ais.ducklake.files/`.
- The `pipeline/v2/` directory referenced by CI doesn't exist in the repo — it's a work-in-progress or separate deployment.
- No test suite exists for this project. It's a technical prototype.
- Environment: copy `.env.template` to `.env` and fill in credentials.
