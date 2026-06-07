# ML ETA Prediction — Plan

## Context

Prédire l'ETA (Estimated Time of Arrival) des bateaux à partir des données AIS.
Problème : pas de ground truth (heure d'arrivée réelle). L'ETA déclarée par l'équipage
est le seul signal disponible dans les messages `ShipStaticData`.

Objectif : reconstruire l'heure d'arrivée réelle depuis les positions AIS, puis entraîner
un modèle qui prédit l'erreur d'ETA (ETA déclarée - arrivée réelle) ou l'ETA directement.

## Données disponibles

| Table | Rows | Période |
|---|---|---|
| `messages` | 72.9M | 2026-05-26 → 2026-05-29 |
| `vessels_positions` | 46.2M | même période |
| `vessels` | 60.9K | snapshot latest |

- `vessels_positions` : lat, lon, sog (speed over ground), cog (course), ts, mmsi
- `messages` : tout, inclut `ShipStaticData` avec `eta` + `destination` (786K ETA non-null)
- Destination quality: ~87% ont une valeur, mais parsing variable ("ROTTERDAM" vs "NLRTM" vs garbage)

## Pipeline global

```
Phase 1: Reconstruct actual arrival times
  1a. Download port database (UN/LOCODE) → port_name → (lat, lon)
  1b. Parse/normalize destination strings → match to ports
  1c. Detect arrival: for each (MMSI, destination), find when vessel stops near port

Phase 2: Build training dataset
  2a. Extract declared ETAs from ShipStaticData
  2b. Join declared ETA ↔ reconstructed actual arrival
  2c. Compute features: distance to dest, SOG, COG, vessel type, length, time features

Phase 3: Train model
  3a. Baseline: XGBoost regression → predict ETA error (actual - declared)
  3b. Evaluate: MAE, RMSE, error distribution by distance/vessel type
  3c. Iterate: features, model type, hyperparams
```

## Phase 1: Arrival Detection (le plus critique)

### 1a. Port database

UN/LOCODE (UNECE) — standard international, ~80K entrées. Téléchargement:
`https://unece.org/trade/cefact/UNLOCODE-Download`

Format: CSV avec `LOCODE`, `Name`, `Coordinates`. Ex: `NLRTM` → Rotterdam.

Script `ml/fetch_ports.py`:
- Download UN/LOCODE CSV
- Parse → `ml/data/ports.parquet` local (lo_code, name, lat, lon, country)
- Fallback: Natural Earth ports shapefile pour les noms sans LOCODE

### 1b. Destination normalization

- Nettoyer: uppercase, strip whitespace, remove non-printable
- Match: exact LOCODE, then fuzzy match on name (Levenshtein < 2)
- Non-matchable → exclure (ou utiliser arrival detection sans geofence)

### 1c. Arrival detection — 2 méthodes combinées

**Méthode A — Speed-based (sans port):**
Pour chaque MMSI, parcourir `vessels_positions` trié par ts:
1. Identifier les "stop events": SOG < 0.5 knots pendant ≥ 30 min consécutives
2. Un stop event = arrivée potentielle
3. Associer à la destination déclarée la plus récente avant le stop

**Méthode B — Geofence (avec port):**
Pour chaque (MMSI, destination normalisée, port_coords):
1. Définir un rayon de port (5-10 NM selon taille du port)
2. Détecter quand le bateau entre dans le rayon ET reste (SOG < 1)
3. Timestamp d'entrée = arrival_time

**Combinaison:**
- Méthode B si port matché → plus précis
- Méthode A sinon → fallback

### Sortie Phase 1

Fichier `ml/data/arrivals.parquet`:
```
mmsi, destination_raw, destination_norm, port_lo_code, port_lat, port_lon,
arrival_ts, arrival_lat, arrival_lon, detection_method (geofence|speed),
declared_eta (from last ShipStaticData before arrival),
static_ts (when ETA was declared),
vessel_name, ship_type, length, width
```

## Phase 2: Training Dataset

Script `ml/build_dataset.py`:

1. Charger `arrivals.parquet`
2. Filtrer: virer les arrivées sans ETA déclarée, sans destination normée
3. Features:
   - **Spatial**: distance_to_dest (km, haversine), bearing_to_dest
   - **Cinématique**: sog, cog, delta_cog (déviation de la route directe)
   - **Vessel**: ship_type, length, width, length/width ratio
   - **Temporel**: hour_of_day, day_of_week, month
   - **Historique**: avg_sog_last_1h, avg_sog_last_6h, distance_traveled_last_6h
   - **Déclaration**: hours_until_eta (= ETA - now), delta_hours_since_last_static
4. Target: `eta_error_hours = (arrival_ts - declared_eta).total_seconds() / 3600`
   - Positif = en retard, négatif = en avance

## Phase 3: Model Training

Script `ml/train.py`:

1. Split: temporel (train sur 05-26→05-28, test sur 05-29)
2. Baseline: XGBoostRegressor
3. Métriques: MAE (heures), RMSE, R²
4. Analyse: erreur par distance, par type de bateau, par port
5. Itération: feature importance, hyperparam tuning (Optuna)

## Fichiers à créer

```
ml/
├── plan.md              # Ce fichier
├── fetch_ports.py       # Download + parse UN/LOCODE → data/ports.parquet
├── detect_arrivals.py   # Phase 1c: arrival detection from positions
├── build_dataset.py     # Phase 2: join ETA + arrivals + features
├── train.py             # Phase 3: XGBoost training + eval
├── utils.py             # Haversine, destination cleaning, DuckDB helpers
└── data/                # Données intermédiaires (gitignored)
    ├── ports.parquet
    ├── arrivals.parquet
    └── dataset.parquet
```

## Dépendances à ajouter

```
# pyproject.toml
xgboost>=2.0
scikit-learn>=1.5
optuna>=4.0       # optionnel, pour tuning
```

## Vérification

1. `fetch_ports.py` → `ports.parquet` avec ≥ 50K ports, couvrant les top destinations
2. `detect_arrivals.py` sur 4 jours → ≥ 5000 arrivals avec port matché
3. `build_dataset.py` → dataset équilibré, pas de fuite temporelle
4. `train.py` → MAE < 12h (baseline naïve: ETA = maintenant + distance/speed ≈ 24-48h d'erreur)
5. Inspection manuelle de 20 arrivées reconstruites pour valider la logique

## Décisions

- **Scope**: Europe du Nord — ports les plus fréquents dans les données
- **Modèle**: XGBoost — baseline rapide, interprétable
- **Format**: Pipeline Python dans `ml/` — scripts standalone, données dans `ml/data/`
