# DuckLake SQL Pipeline v2

Implementation du plan DuckLake SQL dans le dossier `v2` pour éviter de toucher aux fichiers existants à la racine du S3 public.

## 📁 Structure

```
pipeline/v2/
├── README.md                  # Ce fichier
├── Makefile                  # Commandes utiles (make test, make full-pipeline, ...)
├── run_init.sh               # Initialisation du catalogue DuckLake v2
├── run_consolidate.sh        # Consolidation des données (NDJSON.zst → messages)
├── run_derive.sh             # Dérivation des tables gold
├── run_vessels.sh            # Mise à jour de la table vessels
├── run_full_pipeline.sh      # Orchestration complète pour une heure
├── run_full_day.sh           # Orchestration pour une journée complète
├── save_catalog.sh           # Sauvegarde du catalogue local vers S3
├── load_catalog.sh           # Chargement du catalogue depuis S3
├── test_init.sh              # Tests de validation locale
└── sql/
    ├── init_ducklake.sql     # Création du catalogue et des tables
    ├── consolidate.sql       # Insertion des données depuis ais-raw-prod
    ├── derive_tables.sql     # Dérivation des tables gold
    ├── update_vessels.sql    # Mise à jour de la table dimensionnelle
    └── cleanup.sql           # Nettoyage (optionnel)
```

## 🎯 Différences avec v1

- **Catalogue DuckLake**: `s3://ais-public-prod/v2/ais.ducklake` (au lieu de `s3://ais-public-prod/ais.ducklake`)
- **Dossier de fichiers**: `s3://ais-public-prod/v2/ais.ducklake.files/` (au lieu de `s3://ais-public-prod/ais.ducklake.files/`)
- **Aucune modification** des fichiers existants à la racine du S3 public

## ✅ Prérequis

- DuckDB CLI installé (testé avec v1.5.3)
- Accès S3 OVHcloud avec les credentials dans `.env`
- Bucket `ais-raw-prod` accessible (source des données NDJSON.zst)
- Bucket `ais-public-prod` accessible (destination du DuckLake v2)

## 🚀 Quick Start

### 1. Vérifier les prérequis

```bash
# Vérifier DuckDB
duckdb --version

# Vérifier les credentials (dans le .env à la racine du projet)
cat .env | grep -E "OVH_|BUCKET_"
```

### 2. Tester localement (sans S3)

```bash
cd pipeline/v2
./test_init.sh
```

### 3. Initialiser DuckLake v2

```bash
cd pipeline/v2
./run_init.sh
```

> ⚠️ Cette commande crée le catalogue DuckLake localement et sur S3. Le catalogue est automatiquement chargé depuis S3 s'il existe.

### 4. Exécuter le pipeline pour une heure

```bash
# Pour une heure spécifique (recommandé pour GitHub Actions)
./run_full_pipeline.sh 2026-05-29 00

# Pour la date d'hier, heure 00 (par défaut)
./run_full_pipeline.sh
```

### 5. Exécuter le pipeline pour une journée complète

```bash
./run_full_day.sh 2026-05-29
```

### 6. Accès public direct

Le catalogue est accessible publiquement via :
```bash
# Compter les messages
duckdb ducklake:https://ais-public-prod.s3.gra.io.cloud.ovh.net/v2/ais.ducklake -c "SELECT COUNT(*) FROM messages;"

# Requête personnalisée
duckdb ducklake:https://ais-public-prod.s3.gra.io.cloud.ovh.net/v2/ais.ducklake -c "SELECT * FROM vessels_positions LIMIT 10;"
```

## 📋 Détail des Scripts

### run_init.sh
Initialise le catalogue DuckLake v2 et crée toutes les tables. Télécharge automatiquement le catalogue depuis S3 s'il existe déjà.
```bash
./run_init.sh
```

### run_consolidate.sh
Lit les fichiers NDJSON.zst depuis `ais-raw-prod` pour une heure spécifique et insère dans `messages`. Utilise `read_ndjson()` avec `hive_partitioning=true`.
```bash
./run_consolidate.sh 2026-05-29 00  # date, hour
```

### run_derive.sh
Dérive les tables gold depuis `messages` pour une heure spécifique. Filtre par (year, month, day, hour) pour un traitement incrémental.
```bash
./run_derive.sh 2026-05-29 00  # date, hour
```

### run_vessels.sh
Met à jour la table dimensionnelle `vessels` avec les dernières informations statiques via MERGE (upsert).
```bash
./run_vessels.sh
```

### run_full_pipeline.sh
Orchestre l'exécution complète pour une heure: init → consolidate → derive → vessels. Sauvegarde automatiquement le catalogue.
```bash
./run_full_pipeline.sh 2026-05-29 00  # date, hour (default: yesterday 00)
```

### run_full_day.sh
Exécute le pipeline complet pour toutes les 24 heures d'une journée.
```bash
./run_full_day.sh 2026-05-29
```

### save_catalog.sh
Sauvegarde le catalogue local vers S3 avec ACL public-read pour accès direct via HTTP.
```bash
./save_catalog.sh
```

### load_catalog.sh
Télécharge le catalogue depuis S3 vers le local.
```bash
./load_catalog.sh
```

## 📊 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    S3 OVHcloud (ais-public-prod)                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  v2/                                                             │
│  ├── ais.ducklake              ← Catalogue DuckLake (métadonnées) │
│  │                                            │
│  └── ais.ducklake.files/       ← Fichiers de données DuckLake   │
│      ├── messages/            ← Table bronze (toutes les données) │
│      │   ├── year=2024/                                        │
│      │   │   ├── month=06/                                     │
│      │   │   │   └── day=01/                                   │
│      │   │       └── ...parquet files                         │
│      │   └── ...                                              │
│      ├── vessels_positions/   ← Table gold (positions)          │
│      ├── vessel_tracks/        ← Table gold (traces optimisées) │
│      ├── base_stations/       ← Table gold (stations de base)   │
│      ├── aids_to_navigation/  ← Table gold (aides à navigation)  │
│      └── vessels/             ← Table dimensionnelle            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Data Flow                                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ais-raw-prod (NDJSON.zst)                                      │
│       │                                                         │
│       ▼                                                         │
│  ┌─────────────┐    consolidate.sql    ┌─────────────┐         │
│  │ NDJSON.zst  │ ───────────────────► │  messages   │         │
│  │ files       │                        │ (bronze)    │         │
│  └─────────────┘                        └──────┬──────┘         │
│                                                │                 │
│      derive_tables.sql                         │                 │
│        │                                       │                 │
│        ▼                                       ▼                 │
│  ┌─────────────────┐              ┌─────────────────┐          │
│  │ vessels_positions│◄─────────  │ messages        │          │
│  │ vessel_tracks    │             │                 │          │
│  │ base_stations    │             └─────────────────┘          │
│  │ aids_to_navigation│                                          │
│  └─────────────────┘                                          │
│        │                                                           │
│        ▼                                                           │
│  update_vessels.sql                                               │
│        │                                                           │
│        ▼                                                           │
│  ┌─────────────┐                                                    │
│  │  vessels     │  ← Dimension table (updated with MERGE)          │
│  └─────────────┘                                                    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## 📊 Statistiques

- **Fichiers par heure**: ~239 fichiers NDJSON.zst
- **Taille compressée par heure**: ~70 Mo
- **Taille décompressée estimée par heure**: ~300 Mo
- **Catalogue DuckLake**: ~8 Mo
- **Messages par heure**: ~80-100K
- **Total messages (2026-05-29 00-02)**: ~2,6M messages

## 📦 Makefile

Des commandes utiles sont disponibles via `make`:

```bash
cd pipeline/v2

# Voir toutes les commandes
make help

# Initialiser le catalogue
make init

# Exécuter le pipeline pour une heure
make DATE=2026-05-29 HOUR=00 full-pipeline

# Exécuter le pipeline pour une journée
make DATE=2026-05-29 full-day

# Sauvegarder le catalogue
make save

# Charger le catalogue
make load

# Tester l'accès public
make test

# Exécuter une requête personnalisée
make QUERY="SELECT * FROM messages LIMIT 10" query

# Tout nettoyer localement
make clean
```

## 🚀 GitHub Actions

Un workflow est disponible pour exécuter le pipeline automatiquement chaque heure.

### Setup

1. Créer le fichier `.github/workflows/ducklake-hourly.yml` (déjà présent)
2. Ajouter ces **secrets** dans GitHub (Settings → Secrets → Actions):
   - `OVH_REGION` = `gra`
   - `OVH_ENDPOINT` = `https://s3.gra.io.cloud.ovh.net`
   - `OVH_ACCESS_KEY` = votre OVH access key
   - `OVH_SECRET_KEY` = votre OVH secret key
   - `BUCKET_RAW` = `ais-raw-prod`
   - `BUCKET_PUBLIC` = `ais-public-prod`

### Fonctionnement

- **Schedule**: Toutes les heures à la minute 30
- **Cible**: Traite l'heure précédente (ex: à 14:30, traite 13:00-13:59)
- **Timeout**: 60 minutes par job
- **Ressources**: Utilise ubuntu-latest (~7GB RAM)

### Déclenchement manuel

```bash
# Via GitHub UI: Actions → ducklake-hourly → Run workflow
# Ou via CLI:
gh workflow run ducklake-hourly.yml --field date=2026-05-29 --field hour=00
```

## 🔧 Configuration

Toutes les variables sont lues depuis le fichier `.env` à la racine du projet :

```bash
# OVHcloud
OVH_ENDPOINT=https://s3.gra.io.cloud.ovh.net
OVH_REGION=gra
OVH_ACCESS_KEY=xxx
OVH_SECRET_KEY=yyy

# Buckets
BUCKET_RAW=ais-raw-prod
BUCKET_PUBLIC=ais-public-prod
```

## 🧪 Tests

### Test de validation locale

```bash
./test_init.sh
```

Ce script valide :
- La syntaxe SQL des fichiers
- La présence des mots-clés attendus (PARTITIONED BY, read_ndjson, MERGE, etc.)
- La configuration des chemins S3 (v2/)
- Le chargement de l'extension DuckLake
- Le nombre de fichiers attendus

### Test avec S3 réel

```bash
# Tester l'initialisation
./run_init.sh

# Vérifier que le catalogue a été créé
aws s3 ls s3://ais-public-prod/v2/ais.ducklake/

# Tester la consolidation pour une date
duckdb -c "
    INSTALL httpfs;
    LOAD httpfs;
    SET s3_endpoint='s3.gra.io.cloud.ovh.net';
    SET s3_access_key_id='...';
    SET s3_secret_access_key='...';
    ATTACH 's3://ais-public-prod/v2/ais.ducklake' AS ais_lake;
    SELECT COUNT(*) FROM ais_lake.messages WHERE year = 2024 AND month = 6 AND day = 1;
"
```

## 📝 Notes

1. **Pas de conversion** : Les fichiers NDJSON.zst existants sont lus directement par DuckDB via `read_ndjson()`
2. **Partitionnement** : Toutes les tables sont partitionnées par (year, month, day) pour des performances optimales
3. **Dédoublonnage** : La consolidation utilise `QUALIFY ROW_NUMBER() ... = 1` pour garder le premier message reçu
4. **Compatibilité** : Le listener.py existant continue de fonctionner sans modification
5. **Sécurité** : Aucune écriture n'est effectuée dans les dossiers existants (v1)

## 🎉 Migration depuis v1

Pour migrer depuis l'ancien pipeline :

1. Arrêter l'ancien pipeline (`consolidate_optimized.py` + `publish_ducklake.py`)
2. Exécuter `./run_init.sh` pour créer le catalogue v2
3. Rejouer les données historiques avec `./run_full_pipeline.sh <date>` pour chaque date
4. Basculer le frontend pour pointer vers `s3://ais-public-prod/v2/ais.ducklake`
5. Reprendre le pipeline avec les nouvelles dates

## ❓ Pourquoi consolidate ?

DuckDB peut bien lire les fichiers NDJSON.zst directement avec `read_ndjson()`, et c'est exactement ce que fait le script `consolidate.sql`. Cependant, on ne peut pas éviter cette étape car :

1. **Performance**: Lire 239 fichiers séparés est plus lent que requêter une table consolidée
2. **Partitionnement**: La table `messages` est partitionnée par (year, month, day) pour des requêtes efficaces
3. **Dédoublonnage**: `QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi, ts, message_type ORDER BY received_at) = 1` garde seulement le premier message reçu pour chaque (mmsi, ts, message_type)
4. **Schema uniforme**: Convertit toutes les variantes de champs JSON (ex: `MMSI` vs `mmsi`, `Sog` vs `sog`) en un schema cohérent
5. **Indexation**: DuckLake peut optimiser les requêtes sur les tables partitionnées

Le `read_ndjson()` est utilisé directement dans le SQL :
```sql
FROM read_ndjson(
    's3://ais-raw-prod/raw/year=.../month=.../day=.../hour=.../*.ndjson.zst',
    hive_partitioning=true,
    ignore_errors=true
)
```

## 📊 Tableau comparatif v1 vs v2

| Aspect | v1 | v2 |
|--------|-----|-----|
| **Catalogue** | S3 direct (RO) | Local (RW) + S3 sync |
| **Granularité** | Journalière | Horaire |
| **Format données** | NDJSON.zst | Parquet (DuckLake) |
| **Accès public** | Oui | Oui (via HTTP direct) |
| **Partitionnement** | Par jour | Par jour + heure |
| **Dédoublonnage** | Python | SQL (QUALIFY) |
| **GitHub Actions** | Non | Oui (hourly) |
| **Mémoire** | ~12GB/jour | ~4-6GB/heure |
| **Temps** | ~20 min/jour | ~5-10 min/heure |

## 🎯 Bonnes pratiques

1. **Traitement incrémental**: Toujour traiter par heure, pas par jour
2. **Catalogue local**: Ne jamais supprimer `/tmp/ais.ducklake` manuellement - utiliser `load_catalog.sh` pour le recharger depuis S3
3. **Sauvegarde automatique**: Chaque script sauvegarde automatiquement le catalogue via `save_catalog.sh`
4. **Public-read ACL**: Toujours appliquer l'ACL public-read avec `aws s3api put-object-acl --acl public-read`
5. **Test**: Utiliser `duckdb ducklake:https://...` pour vérifier l'accès public

## 📞 Support

- Le catalogue v2 coexiste avec le catalogue v1
- Pas de conflit : chemins S3 différents (`/v2/` vs `/`)
- Possible de revenir en arrière en pointant vers l'ancien catalogue
- L'ancien catalogue reste accessible via `ducklake:https://ais-public-prod.s3.gra.io.cloud.ovh.net/ais.ducklake`
