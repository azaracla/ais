## Plan complet pour implémenter DuckDB-WASM multi-threadé

---

### 1. Prérequis et configuration serveur

#### 1.1. Activer Cross-Origin Isolation (COI)
**Obligatoire** pour SharedArrayBuffer et pthreads.

**Configuration Apache** (`.htaccess` ou `httpd.conf`) :
```apache
Header set Cross-Origin-Opener-Policy "same-origin"
Header set Cross-Origin-Embedder-Policy "require-corp"
```

**Configuration Nginx** :
```nginx
add_header Cross-Origin-Opener-Policy same-origin;
add_header Cross-Origin-Embedder-Policy require-corp;
```

**Configuration Express.js** :
```javascript
app.use((req, res, next) => {
  res.setHeader("Cross-Origin-Opener-Policy", "same-origin");
  res.setHeader("Cross-Origin-Embedder-Policy", "require-corp");
  next();
});
```

**Vérification** :
- Ouvrir les DevTools → Console → Vérifier que `crossOriginIsolated` retourne `true` :
  ```javascript
  console.log(self.crossOriginIsolated); // Doit afficher true
  ```

**Source** : [MDN - Cross-Origin Isolation](https://developer.mozilla.org/en-US/docs/Web/API/crossOriginIsolated)

---

#### 1.2. Vérifier la compatibilité du navigateur
- **Chrome/Edge** : ≥ 91
- **Firefox** : ≥ 90
- **Safari** : ≥ 15.2 (mode privé bloque SharedArrayBuffer)

**Test de compatibilité** :
```javascript
if (!self.crossOriginIsolated || !window.SharedArrayBuffer) {
  throw new Error("Multithreading non supporté : COI ou SharedArrayBuffer manquant");
}
```

**Source** : [Can I Use - SharedArrayBuffer](https://caniuse.com/sharedarraybuffer)

---

### 2. Configuration du projet

#### 2.1. Initialiser le projet
```bash
npm init -y
npm install @duckdb/duckdb-wasm
```

#### 2.2. Configuration TypeScript (optionnel)
```bash
npm install typescript @types/node --save-dev
npx tsc --init
```

**tsconfig.json** :
```json
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "node",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true
  }
}
```

---

### 3. Implémentation du chargement multi-threadé

#### 3.1. Importer les dépendances
```javascript
import * as duckdb from '@duckdb/duckdb-wasm';
```

#### 3.2. Sélectionner le bundle approprié
Utiliser `selectBundle()` pour détecter automatiquement le meilleur variant (inclut `threads` si disponible) :

```javascript
// Récupérer les bundles depuis le CDN officiel (jsDelivr)
const JSDELIVR_BUNDLES = duckdb.getJsDelivrBundles();

// Sélectionner le bundle (choisit 'threads' si COI est activé)
const bundle = await duckdb.selectBundle(JSDELIVR_BUNDLES);
```

**Source** : [DuckDB WASM - Instantiation](https://duckdb.org/docs/stable/clients/wasm/instantiation)

#### 3.3. Créer le Worker et instancier DuckDB
```javascript
// Créer un Worker pour le main module (type: 'module' requis pour ES modules)
const worker = new Worker(bundle.mainWorker, { type: 'module' });

// Initialiser le logger
const logger = new duckdb.ConsoleLogger();

// Créer une instance AsyncDuckDB
const db = new duckdb.AsyncDuckDB(logger, worker);

// Instancier avec le pthreadWorker (obligatoire pour le multithreading)
await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
```

**Source** : [npm @duckdb/duckdb-wasm](https://www.npmjs.com/package/@duckdb/duckdb-wasm)

---
#### 3.4. Exemple complet avec gestion d'erreurs
```javascript
async function initDuckDB() {
  try {
    if (!self.crossOriginIsolated) {
      throw new Error(
        "Cross-Origin Isolation requis. " +
        "Vérifiez les headers COOP/COEP."
      );
    }

    const JSDELIVR_BUNDLES = duckdb.getJsDelivrBundles();
    const bundle = await duckdb.selectBundle(JSDELIVR_BUNDLES);

    const worker = new Worker(bundle.mainWorker, { type: 'module' });
    const logger = new duckdb.ConsoleLogger();
    const db = new duckdb.AsyncDuckDB(logger, worker);

    await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
    return db;
  } catch (error) {
    console.error("Échec de l'initialisation DuckDB-WASM:", error);
    throw error;
  }
}
```

---
### 4. Vérification du multithreading

#### 4.1. Vérifier le nombre de threads disponibles
```javascript
const conn = await db.connect();
const result = await conn.query("SELECT * FROM duckdb_threads();");
console.log("Threads disponibles:", result.toArray());
```

**Source** : [DuckDB - System Tables](https://duckdb.org/docs/sql/system_tables)

#### 4.2. Tester une requête multi-threadée
```javascript
// Forcer l'utilisation de plusieurs threads avec PRAGMA
await conn.query("PRAGMA threads=4;");

// Exécuter une requête lourde (ex: jointure ou agrégation)
const start = performance.now();
const result = await conn.query(`
  SELECT
    COUNT(*) as count,
    AVG(value) as avg_value
  FROM generate_series(1, 1000000) as t(value)
  GROUP BY value % 10
`);
const duration = performance.now() - start;
console.log(`Requête exécutée en ${duration.toFixed(2)}ms`);
```

---
### 5. Déploiement

#### 5.1. Déploiement avec un bundler (Vite, Webpack, Rollup)
**Exemple avec Vite** :
```bash
npm install vite --save-dev
```

**vite.config.js** :
```javascript
import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    headers: {
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
    },
  },
  worker: {
    format: 'es',
  },
});
```

#### 5.2. Déploiement statique (Netlify, Vercel, GitHub Pages)
- **Netlify** : Ajouter les headers dans `_headers` :
  ```
  /*
    Cross-Origin-Opener-Policy: same-origin
    Cross-Origin-Embedder-Policy: require-corp
  ```

- **Vercel** : Ajouter dans `vercel.json` :
  ```json
  {
    "headers": [
      {
        "source": "/(.*)",
        "headers": [
          { "key": "Cross-Origin-Opener-Policy", "value": "same-origin" },
          { "key": "Cross-Origin-Embedder-Policy", "value": "require-corp" }
        ]
      }
    ]
  }
  ```

---
### 6. Dépannage

#### 6.1. Erreurs courantes

| Erreur | Cause | Solution |
|--------|-------|----------|
| `SharedArrayBuffer is not defined` | COI non activé | Vérifier les headers COOP/COEP |
| `Failed to instantiate WebAssembly module` | Bundle incorrect | Utiliser `selectBundle()` |
| `pthreadWorker is required for threaded mode` | `pthreadWorker` manquant | Passer `bundle.pthreadWorker` à `instantiate()` |
| `TypeError: Failed to construct 'Worker'` | Worker non trouvé | Vérifier le chemin du worker (`{ type: 'module' }`) |
| `Out of memory` | Limite WASM (4 GB) | Réduire la taille des données ou utiliser le mode streaming |

#### 6.2. Vérifier la configuration
```javascript
// Vérifier COI
console.log("COI:", self.crossOriginIsolated);

// Vérifier SharedArrayBuffer
console.log("SharedArrayBuffer:", typeof SharedArrayBuffer);

// Vérifier les headers
fetch('/')
  .then(res => {
    console.log("COOP:", res.headers.get('Cross-Origin-Opener-Policy'));
    console.log("COEP:", res.headers.get('Cross-Origin-Embedder-Policy'));
  });
```

---
### 7. Benchmark et optimisation

#### 7.1. Comparer single-thread vs multi-thread
```javascript
async function benchmark(db, query, iterations = 5) {
  const times = [];
  for (let i = 0; i < iterations; i++) {
    const conn = await db.connect();
    const start = performance.now();
    await conn.query(query);
    times.push(performance.now() - start);
    await conn.close();
  }
  const avg = times.reduce((a, b) => a + b, 0) / times.length;
  return { avg, min: Math.min(...times), max: Math.max(...times) };
}

// Test avec 1 thread
await db.query("PRAGMA threads=1;");
const singleThread = await benchmark(db, "SELECT COUNT(*) FROM large_table");

// Test avec 4 threads
await db.query("PRAGMA threads=4;");
const multiThread = await benchmark(db, "SELECT COUNT(*) FROM large_table");

console.log("Single-thread:", singleThread);
console.log("Multi-thread:", multiThread);
```

#### 7.2. Optimiser les requêtes
- Utiliser `PRAGMA threads=N` pour ajuster le nombre de threads.
- Éviter les opérations bloquantes (ex: `ORDER BY` sur de grands jeux de données).
- Utiliser des index ou des partitions pour les tables.

**Source** : [DuckDB - Performance Tips](https://duckdb.org/docs/sql/performance)

---
### 8. Maintenance et mises à jour

#### 8.1. Suivre les versions de DuckDB-WASM
```bash
npm outdated @duckdb/duckdb-wasm
npm update @duckdb/duckdb-wasm
```

#### 8.2. Vérifier les changelogs
- [DuckDB-WASM Releases](https://github.com/duckdb/duckdb-wasm/releases)
- [DuckDB Blog](https://duckdb.org/blog/)

---
---
## Résumé des étapes clés

| Étape | Action | Source |
|-------|--------|--------|
| 1 | Configurer COOP/COEP | [MDN COI](https://developer.mozilla.org/en-US/docs/Web/API/crossOriginIsolated) |
| 2 | Installer `@duckdb/duckdb-wasm` | [npm](https://www.npmjs.com/package/@duckdb/duckdb-wasm) |
| 3 | Sélectionner le bundle avec `selectBundle()` | [DuckDB WASM Docs](https://duckdb.org/docs/stable/clients/wasm/instantiation) |
| 4 | Instancier avec `pthreadWorker` | [DuckDB WASM API](https://shell.duckdb.org/docs/interfaces/index.DuckDBBundles.html) |
| 5 | Vérifier avec `duckdb_threads()` | [DuckDB System Tables](https://duckdb.org/docs/sql/system_tables) |

---
---
## Annexes

### A. Exemple complet (HTML + JS)
```html
<!DOCTYPE html>
<html>
<head>
  <title>DuckDB-WASM Multi-thread</title>
  <script type="module">
    import * as duckdb from 'https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@latest/dist/duckdb-wasm.js';

    async function main() {
      if (!self.crossOriginIsolated) {
        alert("COI requis ! Vérifiez les headers du serveur.");
        return;
      }

      const JSDELIVR_BUNDLES = duckdb.getJsDelivrBundles();
      const bundle = await duckdb.selectBundle(JSDELIVR_BUNDLES);

      const worker = new Worker(bundle.mainWorker, { type: 'module' });
      const logger = new duckdb.ConsoleLogger();
      const db = new duckdb.AsyncDuckDB(logger, worker);

      await db.instantiate(bundle.mainModule, bundle.pthreadWorker);

      const conn = await db.connect();
      await conn.query("PRAGMA threads=4;");
      const result = await conn.query("SELECT * FROM duckdb_threads();");
      console.log("Threads:", result.toArray());
    }

    main().catch(console.error);
  </script>
</head>
<body>
  <h1>DuckDB-WASM Multi-thread Demo</h1>
</body>
</html>
```

### B. Références officielles
1. [DuckDB-WASM GitHub](https://github.com/duckdb/duckdb-wasm)
2. [DuckDB WASM Documentation](https://duckdb.org/docs/current/clients/wasm/overview)
3. [DuckDB WASM - Instantiation Guide](https://duckdb.org/docs/stable/clients/wasm/instantiation)
4. [MDN - Cross-Origin Isolation](https://developer.mozilla.org/en-US/docs/Web/API/crossOriginIsolated)
5. [Can I Use - SharedArrayBuffer](https://caniuse.com/sharedarraybuffer)
