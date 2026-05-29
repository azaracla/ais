# AIS Ship Visualizer

Frontend WebGL performant pour visualiser les données AIS (Automatic Identification System) stockées dans DuckLake via duckdb-wasm.

## Caractéristiques

- **16,69 millions de messages AIS** accessibles via DuckLake
- **Visualisation WebGL** avec Three.js pour des performances optimales
- **Dernière position par navire** (GROUP BY MMSI) pour réduire le nombre de points affichés
- **Flèches de direction** basées sur le COG (Course Over Ground)
- **Timeline** pour filtrer par plage horaire
- **Recherche par MMSI/IMO**
- **Infobulles** avec détails du navire au survol
- **Zoom/Pan** avec OrbitControls
- **Double-clic** pour zoomer sur un navire

## Structure

```
front/
├── index.html          # Point d'entrée
├── package.json        # Dépendances
├── vite.config.js      # Configuration Vite
├── README.md           # Documentation
├── public/             # Assets statiques
└── src/
    ├── main.js         # Logique principale
    ├── duckdb.js       # Intégration DuckDB-WASM
    ├── renderer.js     # Rendu Three.js
    ├── ui.js           # Composants UI
    └── style.css       # Styles
```

## Prérequis

- Node.js v18+
- npm ou yarn

## Installation

```bash
cd front
npm install
```

## Développement

```bash
npm run dev
```

Ouvre http://localhost:3000 dans votre navigateur.

## Production

```bash
npm run build
npm run preview
```

## Utilisation

1. **Timeline** : Sélectionnez une plage horaire (1h, 6h, 12h, 24h, 3j, 7j) ou entrez des dates manuellement
2. **Recherche** : Entrez un MMSI ou IMO pour trouver un navire spécifique
3. **Navigation** : 
   - Gauche-clic + glisser : Déplacer la carte
   - Molette : Zoomer
   - Double-clic : Zoomer sur un navire
4. **Raccourcis clavier** :
   - `ESC` : Fermer l'infobulle
   - `R` : Réinitialiser la vue
   - `F` : Ajuster à toutes les données

## Performances

- **~500K-1M de points** affichés (dernières positions par navire)
- **>60 FPS** sur la plupart des configurations
- **InstancedMesh** pour les flèches de direction
- **WebGL natif** via Three.js

## Technologies

- [Vite](https://vitejs.dev/) - Bundler
- [Three.js](https://threejs.org/) - WebGL
- [DuckDB-WASM](https://duckdb.org/docs/api/wasm) - Base de données dans le navigateur
- [DuckLake](https://duckdb.org/docs/extensions/ducklake) - Extension DuckDB pour S3

## Données

Source : `https://ais-public-prod.s3.gra.io.cloud.ovh.net/ais.ducklake`

Champs utilisés :
- `mmsi` - Maritime Mobile Service Identity
- `lat` / `lon` - Position géographique
- `cog` - Course Over Ground (cap)
- `sog` - Speed Over Ground (vitesse)
- `name` - Nom du navire
- `imo_number` - IMO number
- `message_type` - Type de message AIS
- `ts` - Timestamp

## Optimisations

1. **GROUP BY MMSI** : Une seule position par navire (la plus récente)
2. **Requêtes filtrées** : Par plage horaire et zone géographique visible
3. **InstancedMesh** : Rendu efficace des flèches
4. **Lazy loading** : Chargement des données à la demande

## Problèmes connus

- Le chargement initial de DuckDB-WASM peut prendre quelques secondes
- Les requêtes sur 16M de lignes peuvent être lentes sans filtres
- La précision des positions dépend des données AIS

## Auteur

Arthur

## License

MIT
