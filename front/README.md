# front_v2 — AIS Vessel Tracker

Application React + DuckDB WASM pour visualiser les positions AIS en temps réel via DuckLake.

## DuckDB WASM : HTTP Range Requests

Les fichiers Parquet (113–298 Mo par jour) sont servis depuis OVH S3. DuckDB WASM utilise des **Range Requests** (requêtes partielles HTTP) pour ne télécharger que les row groups nécessaires à la requête.

### Problème

OVH S3 répond mal au `HEAD` avec `Range: bytes=0-` — DuckDB croit que le serveur ne supporte pas les requêtes partielles et **télécharge le fichier entier** (113 Mo+).

Avec la détection HEAD défaillante, `forceFullHTTPReads` par défaut à `true` force le full read.

### CORS S3

Le bucket S3 doit exposer les headers `Content-Range` et `Accept-Ranges` et autoriser le header `Range` pour que le navigateur accepte les réponses 206 Partial Content :

```json
{
  "CORSRules": [{
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["Range"],
    "ExposeHeaders": ["Content-Range", "Accept-Ranges", "Content-Length", "ETag"]
  }]
}
```

(`infra/cors.json`)

### Solution

Dans `src/duckdb.ts:initDuckDB()`, avant `db.connect()` :

```typescript
await db.open({
  filesystem: {
    allowFullHTTPReads: false,   // interdit le fallback full read
    reliableHeadRequests: true,  // OVH S3 répond correctement au HEAD+Range
    forceFullHTTPReads: false,   // crucial : défaut à true si absent
  },
});
```

`forceFullHTTPReads` **doit** être passé explicitement à `false` — s'il est absent, il défaut à `true`, ce qui force le téléchargement complet même avec des Range Requests fonctionnelles.

### Résultat

| Avant | Après |
|---|---|
| 1 requête GET, 113 Mo | ~500 requêtes GET avec `Range: bytes=X-Y`, ~5 Mo |

Le nombre de requêtes et le volume exact dépendent de la fenêtre temporelle et du nombre de row groups Parquet lus.

## Pipeline aval

L'ordre des données dans les fichiers Parquet est crucial : les row groups doivent être triés par `ts ASC, mmsi ASC` pour que DuckDB puisse les skipper via les statistiques min/max.

Voir `publish_ducklake.py` — le `ORDER BY ts ASC, mmsi ASC` garantit que chaque row group couvre ~13 min de données, ce qui permet au filtre `ts BETWEEN` de ne lire que 1–2 row groups.
