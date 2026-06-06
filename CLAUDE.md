# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Serverless AIS maritime traffic viewer. Ingest raw AIS messages via WebSocket → consolidate into Parquet → publish as a DuckLake catalog on public S3. The browser runs DuckDB WASM, attaches the remote DuckLake catalog, and queries Parquet files directly via HTTP Range requests — no backend at query time.

## Stack

- **Storage**: OVHcloud S3 (path-style addressing), public-read bucket
- **Pipeline**: Python (asyncio, boto3, DuckDB, PyArrow, zstandard)
- **Frontend**: React 19 + TypeScript + Maplibre GL + `@duckdb/duckdb-wasm`
- **Satellite proxy**: FastAPI (Python) proxying Google Earth Engine tiles
- **Infra**: Terraform (OVH provider)

## Common Commands

```bash
# Pipeline
pip install -r requirements.txt
docker-compose up -d                          # start listener(s)
python pipeline/consolidate_optimized.py --date YYYY-MM-DD
python pipeline/publish_ducklake.py --date YYYY-MM-DD
python pipeline/publish_ducklake.py --from YYYY-MM-DD --to YYYY-MM-DD --force

# Frontend
cd front && npm install && npm run dev        # Vite dev server
cd front && npm run build                     # production build
cd front && npm run lint                      # ESLint

# Satellite proxy
cd front/services/satellite-proxy && uvicorn main:app --port 8000

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

## Key Conventions

- S3 addressing is **path-style** (`s3.gra.io.cloud.ovh.net/{bucket}/{key}`), not virtual-hosted. Both Python boto3 and DuckDB WASM configs must reflect this.
- Parquet files use **ZSTD compression**, row groups of 100k rows, sorted by `message_type, mmsi, ts`.
- The `vessel_tracks` table stores lat/lon as **integers** (`ROUND(lat * 1e5)`) to reduce Parquet size. Query code divides by `1e5` when reading.
- `consolidate_optimized.py` uses the raw bucket (`BUCKET_RAW`) for both input and output; the output key in `BUCKET_SILVER = BUCKET_RAW` is deliberate — silver files live alongside raw under a `silver/` prefix in the same bucket.
- DuckLake catalog path in the frontend uses `/v2/` prefix (`ais.ducklake` and `ais.ducklake.files/`). This is the current production catalog version.
- The `pipeline/v2/` directory referenced by CI doesn't exist in the repo — it's a work-in-progress or separate deployment.
- No test suite exists for this project. It's a technical prototype.
- Environment: copy `.env.template` to `.env` and fill in credentials.
