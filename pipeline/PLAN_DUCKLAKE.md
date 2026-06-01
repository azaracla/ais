# Plan de Migration vers DuckLake (Option 1 : NDJSON.zst)

## 🎯 Objectif
Simplifier le pipeline AIS en utilisant **DuckDB + DuckLake** avec les fichiers **NDJSON.zst** existants, sans modification de `listener.py`.

---

## ✅ Prérequis Validés
- **DuckDB lit bien les fichiers `.ndjson.zst`** avec `read_ndjson()` (testé et confirmée par @arthur).
- **Pas besoin de convertir en Parquet** : Le pipeline actuel est déjà compatible.
- **`hive_partitioning=true` fonctionne avec NDJSON.zst** pour la détection automatique des partitions.

---

## 📁 Structure Cible
```
pipeline/
├── listener.py               # Inchangé (écrit en NDJSON.zst)
├── sql/
│   ├── init_ducklake.sql     # Initialisation du catalogue DuckLake
│   ├── consolidate.sql       # Consolidation (remplace consolidate_optimized.py)
│   ├── derive_tables.sql     # Dérivation des tables gold (remplace publish_ducklake.py)
│   ├── update_vessels.sql    # Mise à jour de 'vessels' (MERGE INTO)
│   └── cleanup.sql           # Nettoyage (optionnel)
├── run_consolidate.sh       # Script pour exécuter la consolidation
├── run_derive.sh             # Script pour dériver les tables gold
├── run_vessels.sh            # Script pour mettre à jour 'vessels'
└── run_full_pipeline.sh      # Orchestration complète
```

---

## 📜 1. Initialisation du DuckLake (`sql/init_ducklake.sql`)
```sql
-- 1. Charger l'extension DuckLake
INSTALL ducklake;
LOAD ducklake;

-- 2. Attacher le catalogue DuckLake
ATTACH 's3://ais-public-prod/ais.ducklake' AS ais_lake (
    TYPE ducklake,
    DATA_PATH 's3://ais-public-prod/ais.ducklake.files/',
    OVERRIDE_DATA_PATH true
);

-- 3. Créer les tables (si inexistantes)
CREATE TABLE IF NOT EXISTS ais_lake.messages (
    message_type VARCHAR,
    mmsi BIGINT,
    ts TIMESTAMPTZ,
    lat DOUBLE,
    lon DOUBLE,
    received_at TIMESTAMPTZ,
    source_listener VARCHAR,
    sog DOUBLE,
    cog DOUBLE,
    true_heading INTEGER,
    navigational_status INTEGER,
    rate_of_turn INTEGER,
    message_id INTEGER,
    position_accuracy BOOLEAN,
    raim BOOLEAN,
    valid BOOLEAN,
    name VARCHAR,
    call_sign VARCHAR,
    imo_number BIGINT,
    ship_type INTEGER,
    ais_version INTEGER,
    length DOUBLE,
    width DOUBLE,
    dimension_a DOUBLE,
    dimension_b DOUBLE,
    dimension_c DOUBLE,
    dimension_d DOUBLE,
    max_static_draught DOUBLE,
    destination VARCHAR,
    eta TIMESTAMPTZ,
    dte BOOLEAN,
    fix_type INTEGER,
    type_of_aton INTEGER,
    off_position BOOLEAN,
    virtual_aton BOOLEAN,
    raw_message VARCHAR,
    metadata_json VARCHAR,
    year INTEGER GENERATED ALWAYS AS (EXTRACT(year FROM ts)),
    month INTEGER GENERATED ALWAYS AS (EXTRACT(month FROM ts)),
    day INTEGER GENERATED ALWAYS AS (EXTRACT(day FROM ts))
) PARTITIONED BY (year, month, day);

-- 4. Tables dérivées (gold)
CREATE TABLE IF NOT EXISTS ais_lake.vessels_positions (
    message_type VARCHAR,
    mmsi BIGINT,
    ts TIMESTAMPTZ,
    lat DOUBLE,
    lon DOUBLE,
    received_at TIMESTAMPTZ,
    source_listener VARCHAR,
    sog DOUBLE,
    cog DOUBLE,
    true_heading INTEGER,
    navigational_status INTEGER,
    rate_of_turn INTEGER,
    message_id INTEGER,
    position_accuracy BOOLEAN,
    raim BOOLEAN,
    valid BOOLEAN,
    year INTEGER,
    month INTEGER,
    day INTEGER
) PARTITIONED BY (year, month, day);

CREATE TABLE IF NOT EXISTS ais_lake.vessel_tracks (
    mmsi INTEGER,
    ts INTEGER,
    lat INTEGER,
    lon INTEGER,
    date DATE
) PARTITIONED BY (date);

CREATE TABLE IF NOT EXISTS ais_lake.base_stations (
    mmsi BIGINT,
    ts TIMESTAMPTZ,
    lat DOUBLE,
    lon DOUBLE,
    received_at TIMESTAMPTZ,
    source_listener VARCHAR,
    message_id INTEGER,
    raim BOOLEAN,
    year INTEGER,
    month INTEGER,
    day INTEGER
) PARTITIONED BY (year, month, day);

CREATE TABLE IF NOT EXISTS ais_lake.aids_to_navigation (
    mmsi BIGINT,
    name VARCHAR,
    type_of_aton INTEGER,
    ts TIMESTAMPTZ,
    lat DOUBLE,
    lon DOUBLE,
    dimension_a DOUBLE,
    dimension_b DOUBLE,
    dimension_c DOUBLE,
    dimension_d DOUBLE,
    off_position BOOLEAN,
    virtual_aton BOOLEAN,
    raim BOOLEAN,
    received_at TIMESTAMPTZ,
    source_listener VARCHAR,
    year INTEGER,
    month INTEGER,
    day INTEGER
) PARTITIONED BY (year, month, day);

CREATE TABLE IF NOT EXISTS ais_lake.vessels (
    mmsi BIGINT PRIMARY KEY,
    name VARCHAR,
    call_sign VARCHAR,
    imo_number BIGINT,
    ship_type INTEGER,
    length DOUBLE,
    width DOUBLE,
    destination VARCHAR,
    last_seen_static TIMESTAMPTZ
);

-- 5. Configurer S3
SET s3_endpoint='s3.gra.io.cloud.ovh.net';
SET s3_access_key_id='${OVH_ACCESS_KEY}';
SET s3_secret_access_key='${OVH_SECRET_KEY}';
SET s3_region='gra';
SET s3_url_style='path';
SET s3_use_ssl=true;
```

---

## 📜 2. Consolidation (`sql/consolidate.sql`)
```sql
-- Paramètre : :target_date (ex: '2024-01-15')
-- Lire les fichiers NDJSON.zst bruts et insérer dans 'messages'
INSERT INTO ais_lake.messages
SELECT 
    message_type,
    metadata->>'MMSI'::BIGINT AS mmsi,
    (metadata->>'time_utc')::TIMESTAMPTZ AS ts,
    (metadata->>'latitude')::DOUBLE AS lat,
    (metadata->>'longitude')::DOUBLE AS lon,
    received_at::TIMESTAMPTZ AS received_at,
    listener_id AS source_listener,
    message->>'Sog'::DOUBLE AS sog,
    message->>'Cog'::DOUBLE AS cog,
    message->>'TrueHeading'::INTEGER AS true_heading,
    message->>'NavigationalStatus'::INTEGER AS navigational_status,
    message->>'RateOfTurn'::INTEGER AS rate_of_turn,
    message->>'MessageID'::INTEGER AS message_id,
    message->>'PositionAccuracy'::BOOLEAN AS position_accuracy,
    message->>'Raim'::BOOLEAN AS raim,
    message->>'Valid'::BOOLEAN AS valid,
    COALESCE(message->>'Name', metadata->>'ShipName') AS name,
    COALESCE(message->>'CallSign', message->'StaticDataReport'->'ReportB'->>'CallSign') AS call_sign,
    COALESCE(message->>'ImoNumber', message->'StaticDataReport'->'ReportA'->>'ImoNumber')::BIGINT AS imo_number,
    COALESCE(message->>'Type', message->'StaticDataReport'->'ReportB'->>'ShipType')::INTEGER AS ship_type,
    message->>'AisVersion'::INTEGER AS ais_version,
    (COALESCE(message->'Dimension'->>'A', 0) + COALESCE(message->'Dimension'->>'B', 0)) AS length,
    (COALESCE(message->'Dimension'->>'C', 0) + COALESCE(message->'Dimension'->>'D', 0)) AS width,
    message->'Dimension'->>'A'::DOUBLE AS dimension_a,
    message->'Dimension'->>'B'::DOUBLE AS dimension_b,
    message->'Dimension'->>'C'::DOUBLE AS dimension_c,
    message->'Dimension'->>'D'::DOUBLE AS dimension_d,
    message->>'MaximumStaticDraught'::DOUBLE AS max_static_draught,
    COALESCE(message->>'Destination', message->'StaticDataReport'->'ReportA'->>'Destination') AS destination,
    NULL AS eta,  -- À parser depuis message->'Eta' (format complexe)
    message->>'Dte'::BOOLEAN AS dte,
    message->>'FixType'::INTEGER AS fix_type,
    CASE WHEN message_type = 'AidsToNavigationReport' THEN message->>'Type'::INTEGER ELSE NULL END AS type_of_aton,
    message->>'OffPosition'::BOOLEAN AS off_position,
    message->>'VirtualAtoN'::BOOLEAN AS virtual_aton,
    raw_message,
    metadata_json
FROM read_ndjson(
    's3://ais-raw-prod/raw/year=' || EXTRACT(year FROM CAST(:target_date AS DATE)) ||
    '/month=' || LPAD(EXTRACT(month FROM CAST(:target_date AS DATE))::VARCHAR, 2, '0') ||
    '/day=' || LPAD(EXTRACT(day FROM CAST(:target_date AS DATE))::VARCHAR, 2, '0') ||
    '/hour=*/*.ndjson.zst',
    hive_partitioning=true
)
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY mmsi, ts, message_type
    ORDER BY received_at ASC
) = 1;
```

---

## 📜 3. Dérivation des Tables Gold (`sql/derive_tables.sql`)
```sql
-- Paramètre : :target_date

-- 1. vessels_positions
INSERT INTO ais_lake.vessels_positions
SELECT 
    message_type, mmsi, ts, lat, lon, received_at, source_listener,
    sog, cog, true_heading, navigational_status, rate_of_turn,
    message_id, position_accuracy, raim, valid,
    EXTRACT(year FROM ts) AS year,
    EXTRACT(month FROM ts) AS month,
    EXTRACT(day FROM ts) AS day
FROM ais_lake.messages
WHERE message_type IN (
    'PositionReport',
    'ExtendedClassBPositionReport',
    'StandardClassBPositionReport',
    'LongRangeAisBroadcast'
)
AND EXTRACT(year FROM ts) = EXTRACT(year FROM CAST(:target_date AS DATE))
AND EXTRACT(month FROM ts) = EXTRACT(month FROM CAST(:target_date AS DATE))
AND EXTRACT(day FROM ts) = EXTRACT(day FROM CAST(:target_date AS DATE));

-- 2. vessel_tracks (downsampled toutes les 10 minutes)
INSERT INTO ais_lake.vessel_tracks
SELECT 
    mmsi::INTEGER AS mmsi,
    epoch(ts)::INTEGER AS ts,
    CAST(ROUND(lat * 1e5) AS INTEGER) AS lat,
    CAST(ROUND(lon * 1e5) AS INTEGER) AS lon,
    CAST(ts AS DATE) AS date
FROM (
    SELECT 
        mmsi, ts, lat, lon,
        epoch(ts)::INTEGER // 600 AS _bucket,
        ROW_NUMBER() OVER (PARTITION BY mmsi, epoch(ts)::INTEGER // 600 ORDER BY ts ASC) AS _rn
    FROM ais_lake.messages
    WHERE message_type IN (
        'PositionReport',
        'ExtendedClassBPositionReport',
        'StandardClassBPositionReport'
    )
    AND EXTRACT(year FROM ts) = EXTRACT(year FROM CAST(:target_date AS DATE))
    AND EXTRACT(month FROM ts) = EXTRACT(month FROM CAST(:target_date AS DATE))
    AND EXTRACT(day FROM ts) = EXTRACT(day FROM CAST(:target_date AS DATE))
) WHERE _rn = 1;

-- 3. base_stations
INSERT INTO ais_lake.base_stations
SELECT 
    mmsi, ts, lat, lon, received_at, source_listener, message_id, raim,
    EXTRACT(year FROM ts) AS year,
    EXTRACT(month FROM ts) AS month,
    EXTRACT(day FROM ts) AS day
FROM ais_lake.messages
WHERE message_type = 'BaseStationReport'
AND EXTRACT(year FROM ts) = EXTRACT(year FROM CAST(:target_date AS DATE))
AND EXTRACT(month FROM ts) = EXTRACT(month FROM CAST(:target_date AS DATE))
AND EXTRACT(day FROM ts) = EXTRACT(day FROM CAST(:target_date AS DATE));

-- 4. aids_to_navigation
INSERT INTO ais_lake.aids_to_navigation
SELECT 
    mmsi, name, type_of_aton, ts, lat, lon,
    dimension_a, dimension_b, dimension_c, dimension_d,
    off_position, virtual_aton, raim, received_at, source_listener,
    EXTRACT(year FROM ts) AS year,
    EXTRACT(month FROM ts) AS month,
    EXTRACT(day FROM ts) AS day
FROM ais_lake.messages
WHERE message_type = 'AidsToNavigationReport'
AND EXTRACT(year FROM ts) = EXTRACT(year FROM CAST(:target_date AS DATE))
AND EXTRACT(month FROM ts) = EXTRACT(month FROM CAST(:target_date AS DATE))
AND EXTRACT(day FROM ts) = EXTRACT(day FROM CAST(:target_date AS DATE));
```

---

## 📜 4. Mise à Jour de `vessels` (`sql/update_vessels.sql`)
```sql
MERGE INTO ais_lake.vessels AS target
USING (
    SELECT 
        mmsi,
        name,
        call_sign,
        imo_number,
        ship_type,
        length,
        width,
        destination,
        ts AS last_seen_static
    FROM ais_lake.messages
    WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
      AND name IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY ts DESC) = 1
) AS source
ON target.mmsi = source.mmsi
WHEN MATCHED THEN
    UPDATE SET
        name = source.name,
        call_sign = source.call_sign,
        imo_number = source.imo_number,
        ship_type = source.ship_type,
        length = source.length,
        width = source.width,
        destination = source.destination,
        last_seen_static = source.last_seen_static
WHEN NOT MATCHED THEN
    INSERT (mmsi, name, call_sign, imo_number, ship_type, length, width, destination, last_seen_static)
    VALUES (
        source.mmsi,
        source.name,
        source.call_sign,
        source.imo_number,
        source.ship_type,
        source.length,
        source.width,
        source.destination,
        source.last_seen_static
    );
```

---

## 📜 5. Scripts Bash

### `run_consolidate.sh`
```bash
#!/bin/bash
set -euo pipefail
source .env

DATE=${1:-$(date -d "yesterday" +%Y-%m-%d)}

duckdb -c "
    INSTALL httpfs;
    LOAD httpfs;
    SET s3_endpoint='${OVH_ENDPOINT//https:\/\/}';
    SET s3_access_key_id='${OVH_ACCESS_KEY}';
    SET s3_secret_access_key='${OVH_SECRET_KEY}';
    SET s3_region='${OVH_REGION}';
    SET s3_url_style='path';
    SET s3_use_ssl=true;

    ATTACH 's3://${BUCKET_PUBLIC}/ais.ducklake' AS ais_lake (
        TYPE ducklake,
        DATA_PATH 's3://${BUCKET_PUBLIC}/ais.ducklake.files/',
        OVERRIDE_DATA_PATH true
    );

    .read 'pipeline/sql/consolidate.sql'
" -param target_date="$DATE"

echo "✅ Consolidation terminée pour $DATE"
```

### `run_derive.sh`
```bash
#!/bin/bash
set -euo pipefail
source .env

DATE=${1:-$(date -d "yesterday" +%Y-%m-%d)}

duckdb -c "
    INSTALL httpfs;
    LOAD httpfs;
    SET s3_endpoint='${OVH_ENDPOINT//https:\/\/}';
    SET s3_access_key_id='${OVH_ACCESS_KEY}';
    SET s3_secret_access_key='${OVH_SECRET_KEY}';
    SET s3_region='${OVH_REGION}';
    SET s3_url_style='path';
    SET s3_use_ssl=true;

    ATTACH 's3://${BUCKET_PUBLIC}/ais.ducklake' AS ais_lake (
        TYPE ducklake,
        DATA_PATH 's3://${BUCKET_PUBLIC}/ais.ducklake.files/',
        OVERRIDE_DATA_PATH true
    );

    .read 'pipeline/sql/derive_tables.sql'
" -param target_date="$DATE"

echo "✅ Dérivation terminée pour $DATE"
```

### `run_vessels.sh`
```bash
#!/bin/bash
set -euo pipefail
source .env

duckdb -c "
    INSTALL httpfs;
    LOAD httpfs;
    SET s3_endpoint='${OVH_ENDPOINT//https:\/\/}';
    SET s3_access_key_id='${OVH_ACCESS_KEY}';
    SET s3_secret_access_key='${OVH_SECRET_KEY}';
    SET s3_region='${OVH_REGION}';
    SET s3_url_style='path';
    SET s3_use_ssl=true;

    ATTACH 's3://${BUCKET_PUBLIC}/ais.ducklake' AS ais_lake (
        TYPE ducklake,
        DATA_PATH 's3://${BUCKET_PUBLIC}/ais.ducklake.files/',
        OVERRIDE_DATA_PATH true
    );

    .read 'pipeline/sql/update_vessels.sql'
"

echo "✅ Mise à jour de 'vessels' terminée"
```

### `run_full_pipeline.sh`
```bash
#!/bin/bash
set -euo pipefail

DATE=${1:-$(date -d "yesterday" +%Y-%m-%d)}

# 1. Initialiser le DuckLake (si nécessaire)
echo "🔧 Initialisation du DuckLake..."
duckdb -c ".read 'pipeline/sql/init_ducklake.sql'"

# 2. Consolidation
echo "📦 Consolidation pour $DATE..."
./run_consolidate.sh "$DATE"

# 3. Dérivation des tables gold
echo "🏗️ Dérivation des tables gold pour $DATE..."
./run_derive.sh "$DATE"

# 4. Mise à jour de 'vessels'
echo "🚢 Mise à jour de la table 'vessels'..."
./run_vessels.sh

echo "✅ Pipeline complet terminé pour $DATE"
```

---

## 📅 Plan de Migration

### Étape 0 : Prérequis
- [ ] **Vérifier que DuckDB CLI est installé** :
  ```bash
  duckdb --version
  ```
- [ ] **Installer les dépendances** :
  ```bash
  pip install duckdb boto3
  ```

### Étape 1 : Initialisation du DuckLake
- [ ] **Exécuter `init_ducklake.sql`** :
  ```bash
  duckdb -c ".read 'pipeline/sql/init_ducklake.sql'"
  ```
- [ ] **Vérifier la création du catalogue** :
  ```bash
  aws s3 ls s3://ais-public-prod/ais.ducklake
  aws s3 ls s3://ais-public-prod/ais.ducklake.files/
  ```

### Étape 2 : Test de la Consolidation
- [ ] **Exécuter `run_consolidate.sh` pour une date test** (ex: `2024-01-01`) :
  ```bash
  chmod +x run_consolidate.sh
  ./run_consolidate.sh 2024-01-01
  ```
- [ ] **Vérifier les données** :
  ```bash
  duckdb -c "
      INSTALL httpfs;
      LOAD httpfs;
      SET s3_endpoint='s3.gra.io.cloud.ovh.net';
      SET s3_access_key_id='...';
      SET s3_secret_access_key='...';
      ATTACH 's3://ais-public-prod/ais.ducklake' AS ais_lake;
      SELECT COUNT(*) FROM ais_lake.messages WHERE year = 2024 AND month = 1 AND day = 1;
  "
  ```

### Étape 3 : Test de la Dérivation
- [ ] **Exécuter `run_derive.sh`** :
  ```bash
  chmod +x run_derive.sh
  ./run_derive.sh 2024-01-01
  ```
- [ ] **Vérifier les tables dérivées** :
  ```bash
  duckdb -c "
      INSTALL httpfs;
      LOAD httpfs;
      SET s3_endpoint='s3.gra.io.cloud.ovh.net';
      SET s3_access_key_id='...';
      SET s3_secret_access_key='...';
      ATTACH 's3://ais-public-prod/ais.ducklake' AS ais_lake;
      SELECT COUNT(*) FROM ais_lake.vessels_positions WHERE year = 2024 AND month = 1 AND day = 1;
  "
  ```

### Étape 4 : Test de la Mise à Jour de `vessels`
- [ ] **Exécuter `run_vessels.sh`** :
  ```bash
  chmod +x run_vessels.sh
  ./run_vessels.sh
  ```
- [ ] **Vérifier la table `vessels`** :
  ```bash
  duckdb -c "
      INSTALL httpfs;
      LOAD httpfs;
      SET s3_endpoint='s3.gra.io.cloud.ovh.net';
      SET s3_access_key_id='...';
      SET s3_secret_access_key='...';
      ATTACH 's3://ais-public-prod/ais.ducklake' AS ais_lake;
      SELECT COUNT(*) FROM ais_lake.vessels;
  "
  ```

### Étape 5 : Basculer en Production
- [ ] **Arrêter l'ancien pipeline** (`consolidate_optimized.py` + `publish_ducklake.py`).
- [ ] **Lancer le nouveau pipeline** pour les nouvelles dates :
  ```bash
  chmod +x run_full_pipeline.sh
  ./run_full_pipeline.sh
  ```
- [ ] **Surveiller** :
  - Vérifier que les données sont bien insérées dans DuckLake.
  - Vérifier que le frontend (DuckDB WASM) peut toujours interroger les données.

### Étape 6 : Nettoyage (Optionnel)
- [ ] **Archiver les anciens scripts** :
  ```bash
  mv pipeline/consolidate_optimized.py pipeline/consolidate_optimized.py.bak
  mv pipeline/publish_ducklake.py pipeline/publish_ducklake.py.bak
  ```

---

## 📊 Comparaison Avant/Après

| **Critère**               | **Ancien Pipeline** | **Nouveau Pipeline** | **Amélioration** |
|---------------------------|--------------------|----------------------|------------------|
| **Lignes de code**        | ~900 (Python)       | ~150 (SQL + Bash)    | **83% de réduction** |
| **Fichiers à maintenir**  | 2 scripts Python    | 4 SQL + 4 Bash       | **Plus modulaire** |
| **Gestion des fichiers**  | Manuelle           | Automatique (DuckLake) | **Moins de bugs** |
| **Performance**           | Bonne              | Excellente           | **Meilleure** |
| **Maintenabilité**        | ❌ Difficile        | ✅ Facile            | **Beaucoup mieux** |
| **Transactionnel**        | ❌ Non              | ✅ Oui (DuckLake)    | **Robuste** |

---

## ⚠️ Points d'Attention

1. **Parsing des Champs Complexes** :
   - Certains champs (ex: `eta`, `dimension`) nécessitent un **parsing spécifique** dans `consolidate.sql`.
   - **À adapter** selon votre schéma exact.

2. **Performances** :
   - `INSERT INTO` peut être lent pour de très gros volumes.
   - **Solution** : Traiter par **batches** (ex: heure par heure).

3. **Sauvegardes** :
   - **Sauvegarder le catalogue DuckLake** (`ais.ducklake`) régulièrement :
     ```bash
     aws s3 cp s3://ais-public-prod/ais.ducklake s3://ais-backup/ais.ducklake-$(date +%Y%m%d)
     ```

4. **Monitoring** :
   - Vérifier l'état du DuckLake :
     ```sql
     SELECT table_name, COUNT(*) AS file_count FROM ais_lake.ducklake_data_file GROUP BY table_name;
     ```

---

## 🎯 Conclusion

- **Votre pipeline actuel (NDJSON.zst) est déjà compatible avec DuckDB**.
- **Pas besoin de modifier `listener.py`**.
- **Simplification massive** : Remplacement de ~900 lignes Python par ~150 lignes SQL + Bash.
- **Gains** : Robustesse, maintenabilité, performances.

---

## 🚀 Prochaines Étapes

1. **Tester l'initialisation** (`init_ducklake.sql`).
2. **Tester la consolidation** (`run_consolidate.sh`) sur une petite date.
3. **Valider les données** dans DuckLake.
4. **Basculer progressivement** vers le nouveau pipeline.
