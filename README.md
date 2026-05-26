# DuckLake Public AIS

Projet de collecte et de consolidation de données AIS (Automatic Identification System) à l'échelle mondiale, utilisant DuckDB et DuckLake sur OVHcloud Object Storage.

## Architecture

1.  **Ingestion (listener.py)**: Écoute les flux WebSocket AISstream, compresse les messages en NDJSON.zst et les stocke dans un bucket S3 "raw".
2.  **Consolidation (consolidate.py)**: Lit les données brutes, les déduplique, les transforme et les compacte au format DuckLake (Parquet) pour une utilisation analytique.
3.  **Stockage**: Utilise OVHcloud Object Storage (compatible S3).
4.  **Déploiement**: Conteneurisé avec Docker et infrastructure gérée par Terraform.

## Configuration

1.  Copiez `.env.template` vers `.env` et remplissez les variables :
    ```bash
    cp .env.template .env
    ```
2.  Installez les dépendances :
    ```bash
    pip install -r requirements.txt
    ```

## Utilisation

### Docker

Lancez les listeners pour les 4 quadrants mondiaux :
```bash
docker-compose up -d
```

### Consolidation Manuelle

Exécutez le script de consolidation :
```bash
python consolidate.py
```

## Infrastructure (Terraform)

Initialisez et appliquez la configuration Terraform :
```bash
terraform init
terraform apply
```

## Limitations & Disclaimer Légal

*   **Best-Effort**: Absence de garantie absolue de couverture mondiale ou de disponibilité.
*   **Usage non-opérationnel**: Ce jeu de données n'est pas conçu pour un usage opérationnel, sécuritaire ou d'assistance à la navigation maritime.
