import * as duckdb from "@duckdb/duckdb-wasm";
import duckdb_wasm_eh from "@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url";
import duckdb_wasm_coi from "@duckdb/duckdb-wasm/dist/duckdb-coi.wasm?url";
import DuckDBWorkerEH from "@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?worker";
import DuckDBWorkerCOI from "@duckdb/duckdb-wasm/dist/duckdb-browser-coi.worker.js?worker";
import DuckDBPThreadWorkerURL from "@duckdb/duckdb-wasm/dist/duckdb-browser-coi.pthread.worker.js?url";
import type { Vessel, Bounds } from "./types";
import { shipTypeAISToCategory } from "./types";

// Instance principale (pour l'application, avec ducklake)
let db: duckdb.AsyncDuckDB | null = null;
let conn: duckdb.AsyncDuckDBConnection | null = null;
let initPromise: Promise<void> | null = null;
let querySeq = 0;

// Mode d'initialisation : threads ou eh (embedded http)
let initMode: 'threads' | 'eh' | null = null;

// Instance pour le benchmark (sans ducklake, peut utiliser le multithreading)
let benchmarkDb: duckdb.AsyncDuckDB | null = null;
let benchmarkConn: duckdb.AsyncDuckDBConnection | null = null;
let benchmarkInitPromise: Promise<void> | null = null;
let benchmarkMode: 'threads' | 'eh' | null = null;

// Vérifie si le multithreading est supporté (COI + SharedArrayBuffer)
export function isMultiThreadSupported(): boolean {
  return typeof self !== 'undefined' && 
    self.crossOriginIsolated && 
    typeof SharedArrayBuffer !== 'undefined';
}

// Retourne le mode d'initialisation utilisé
export function getInitMode(): 'threads' | 'eh' | null {
  return initMode;
}

export function cancelQuery(): Promise<boolean> {
  const result = conn?.cancelSent() ?? Promise.resolve(false);
  result.then((cancelled) => {
    if (cancelled) console.log(`[DuckDB] ⏹ q#${querySeq} cancelled`);
  });
  return result;
}

export function isReady() {
  return db !== null && conn !== null;
}

export async function initDuckDB(forceThreads?: boolean): Promise<void> {
  if (initPromise) return initPromise;

  initPromise = (async () => {
    let worker: Worker;
    let moduleUrl: string;
    let pthreadWorker: string | undefined;

    // Essayer le mode multi-thread si COI est disponible ou si explicitement forcé
    // Par défaut, on essaie le multithreading si disponible
    const useThreads = (forceThreads || isMultiThreadSupported());

    if (useThreads) {
      try {
        // Essayer d'utiliser les bundles COI pour le multithreading
        worker = new DuckDBWorkerCOI();
        moduleUrl = duckdb_wasm_coi;
        pthreadWorker = DuckDBPThreadWorkerURL;
        initMode = 'threads';
        
        console.log('[DuckDB] Initializing in THREADED mode with COI bundle');
      } catch (e) {
        console.warn('[DuckDB] Threaded mode with COI bundle failed, falling back to EH:', e);
        // Tombe en arrière sur EH
        worker = new DuckDBWorkerEH();
        moduleUrl = duckdb_wasm_eh;
        pthreadWorker = undefined;
        initMode = 'eh';
      }
    } else {
      // Mode EH (Embedded HTTP) - fallback
      worker = new DuckDBWorkerEH();
      moduleUrl = duckdb_wasm_eh;
      pthreadWorker = undefined;
      initMode = 'eh';
      console.log('[DuckDB] Initializing in EH mode (COI not available)');
    }

    db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
    
    // Instancier avec ou sans pthreadWorker
    const instantiatePromise = pthreadWorker 
      ? db.instantiate(moduleUrl, pthreadWorker) 
      : db.instantiate(moduleUrl, "?modulePath=");
    
    await instantiatePromise;

    // forceFullHTTPReads doit être explicitement false (défaut: true).
    // OVH S3 répond correctement au HEAD+Range → reliableHeadRequests: true.
    await db.open({
      filesystem: {
        allowFullHTTPReads: false,
        reliableHeadRequests: true,
        forceFullHTTPReads: false,
      },
    });

    conn = await db.connect();

    // Configurer le nombre de threads si en mode threadé
    if (initMode === 'threads') {
      try {
        // Détecter le nombre de threads disponibles
        // Note: Dans le bundle COI, c'est duckdb_types au lieu de duckdb_threads
        const threadsResult = await conn.query("SELECT * FROM duckdb_types();");
        const threads = threadsResult.toArray();
        console.log(`[DuckDB] Threaded mode active with ${threads.length} types (COI bundle)`);
        
        // Configurer pour utiliser tous les threads disponibles
        const coreCount = navigator.hardwareConcurrency || 4;
        await conn.query(`PRAGMA threads=${Math.min(coreCount, 8)};`);
      } catch (e) {
        console.warn('[DuckDB] Could not query threads:', e);
      }
    }

    await conn.query("SET enable_object_cache=true;");
    
    // Essayer d'attacher ducklake - ça peut échouer avec le bundle COI
    try {
      await conn.query(
        "ATTACH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/ais.ducklake' AS ais (TYPE ducklake)"
      );
      const r = await conn.query("SELECT COUNT(*) as cnt FROM ais.vessels_positions LIMIT 1;");
      const cnt = r.toArray()[0]?.cnt ?? 0;
      console.log(`[DuckDB] Initialized (${initMode}). Records:`, cnt);
    } catch (e: any) {
      // Si on est en mode threads et que ducklake échoue, on doit tomber en arrière sur EH
      if (initMode === 'threads') {
        console.warn('[DuckDB] ducklake not supported with COI bundle, falling back to EH mode...', e.message);
        
        // Fermer la connexion actuelle
        try {
          await conn!.close();
        } catch (e2) {
          console.warn('[DuckDB] Error closing connection:', e2);
        }
        
        // Réessayer avec EH
        const worker = new DuckDBWorkerEH();
        const ehDb = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
        await ehDb.instantiate(duckdb_wasm_eh, "?modulePath=");
        await ehDb.open({
          filesystem: {
            allowFullHTTPReads: false,
            reliableHeadRequests: true,
            forceFullHTTPReads: false,
          },
        });
        conn = await ehDb.connect();
        db = ehDb;
        initMode = 'eh';
        
        // Réessayer l'attachement avec EH
        await conn.query("SET enable_object_cache=true;");
        await conn.query(
          "ATTACH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/ais.ducklake' AS ais (TYPE ducklake)"
        );
        const r = await conn.query("SELECT COUNT(*) as cnt FROM ais.vessels_positions LIMIT 1;");
        const cnt = r.toArray()[0]?.cnt ?? 0;
        console.log(`[DuckDB] Initialized (${initMode} after fallback). Records:`, cnt);
      } else {
        // En mode EH, c'est une erreur inattendue
        throw e;
      }
    }
  })();

  return initPromise;
}

// Réinitialiser DuckDB avec un mode spécifique (utile pour les benchmarks)
export async function reinitDuckDB(mode: 'threads' | 'eh'): Promise<void> {
  // Fermer la connexion existante
  if (conn) {
    try {
      await conn.close();
    } catch (e) {
      console.warn('[DuckDB] Error closing connection:', e);
    }
  }
  
  // Réinitialiser toutes les variables
  // Note: db.close() n'existe pas, on se contente de réinitialiser les références
  // Le worker sera terminé automatiquement par le garbage collector
  conn = null;
  db = null;
  initPromise = null;
  initMode = null;
  
  // Forcer le mode souhaité
  return initDuckDB(mode === 'threads');
}

// Initialiser une instance DuckDB dédiée au benchmark (sans ducklake)
// Cette instance peut utiliser le multithreading sans problème de compatibilité
export async function initBenchmarkDuckDB(mode: 'threads' | 'eh' = 'threads'): Promise<void> {
  if (benchmarkInitPromise) return benchmarkInitPromise;

  benchmarkInitPromise = (async () => {
    let worker: Worker;
    let moduleUrl: string;
    let pthreadWorker: string | undefined;
    const useThreads = mode === 'threads' && isMultiThreadSupported();

    if (useThreads) {
      try {
        worker = new DuckDBWorkerCOI();
        moduleUrl = duckdb_wasm_coi;
        pthreadWorker = DuckDBPThreadWorkerURL;
        benchmarkMode = 'threads';
        console.log('[DuckDB] Benchmark DB: Initializing in THREADED mode');
      } catch (e) {
        console.warn('[DuckDB] Benchmark DB: Threaded mode failed, falling back to EH:', e);
        worker = new DuckDBWorkerEH();
        moduleUrl = duckdb_wasm_eh;
        pthreadWorker = undefined;
        benchmarkMode = 'eh';
      }
    } else {
      worker = new DuckDBWorkerEH();
      moduleUrl = duckdb_wasm_eh;
      pthreadWorker = undefined;
      benchmarkMode = 'eh';
      console.log('[DuckDB] Benchmark DB: Initializing in EH mode');
    }

    benchmarkDb = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
    
    const instantiatePromise = pthreadWorker 
      ? benchmarkDb.instantiate(moduleUrl, pthreadWorker) 
      : benchmarkDb.instantiate(moduleUrl, "?modulePath=");
    
    await instantiatePromise;

    // Ouvrir sans attacher ducklake
    // (le benchmark n'a pas besoin d'accéder à des fichiers externes)
    await benchmarkDb.open({});
    
    benchmarkConn = await benchmarkDb.connect();

    // Configurer le nombre de threads si en mode threadé
    if (benchmarkMode === 'threads') {
      try {
        // Le bundle COI n'a pas duckdb_threads, mais on peut quand même configurer PRAGMA threads
        const coreCount = navigator.hardwareConcurrency || 4;
        await benchmarkConn.query(`PRAGMA threads=${Math.min(coreCount, 8)};`);
        console.log(`[DuckDB] Benchmark DB: Threaded mode active, configured with ${Math.min(coreCount, 8)} threads`);
      } catch (e) {
        console.warn('[DuckDB] Benchmark DB: Could not configure threads:', e);
      }
    }

    await benchmarkConn.query("SET enable_object_cache=true;");
    console.log(`[DuckDB] Benchmark DB initialized (${benchmarkMode})`);
  })();

  return benchmarkInitPromise;
}

// Fermer la base de données de benchmark
export async function closeBenchmarkDuckDB(): Promise<void> {
  if (benchmarkConn) {
    try {
      await benchmarkConn.close();
    } catch (e) {
      console.warn('[DuckDB] Error closing benchmark connection:', e);
    }
  }
  benchmarkConn = null;
  benchmarkDb = null;
  benchmarkInitPromise = null;
  benchmarkMode = null;
}

// Obtient le mode de la base de données de benchmark
export function getBenchmarkMode(): 'threads' | 'eh' | null {
  return benchmarkMode;
}

// Exécute un benchmark sur une requête (utilise la base de données de benchmark)
export async function benchmarkQueryOnBenchmarkDB(
  query: string,
  iterations: number = 5
): Promise<BenchmarkResult> {
  if (!benchmarkConn) {
    await initBenchmarkDuckDB('threads');
  }
  if (!benchmarkConn || !benchmarkMode) throw new Error("Benchmark DuckDB not initialized");

  const times: number[] = [];
  
  for (let i = 0; i < iterations; i++) {
    const start = performance.now();
    await benchmarkConn.query(query);
    const duration = performance.now() - start;
    times.push(duration);
  }

  const avg = times.reduce((a, b) => a + b, 0) / times.length;
  
  return {
    query,
    mode: benchmarkMode,
    times,
    avg,
    min: Math.min(...times),
    max: Math.max(...times),
    iterations,
  };
}

// Crée une table de test pour le benchmark (utilise la base de données de benchmark)
export async function createBenchmarkTableOnBenchmarkDB(rowCount: number = 1000000): Promise<void> {
  if (!benchmarkConn) {
    await initBenchmarkDuckDB('threads');
  }
  if (!benchmarkConn) throw new Error("Benchmark DuckDB not initialized");
  
  await benchmarkConn.query(`
    CREATE OR REPLACE TABLE benchmark_data AS
    SELECT 
      i as id,
      random() * 100 as value1,
      random() * 1000 as value2,
      'type_' || (random() * 10)::integer as category,
      CAST(date '2024-01-01' AS date) + (random() * 365)::integer * INTERVAL 1 day as date_val
    FROM generate_series(1, ${rowCount}) as i
  `);
}

// Supprime la table de benchmark (utilise la base de données de benchmark)
export async function dropBenchmarkTableOnBenchmarkDB(): Promise<void> {
  if (!benchmarkConn) {
    await initBenchmarkDuckDB();
  }
  if (!benchmarkConn) throw new Error("Benchmark DuckDB not initialized");
  
  try {
    await benchmarkConn.query("DROP TABLE IF EXISTS benchmark_data;");
  } catch (e) {
    console.warn('[DuckDB] Error dropping benchmark table on benchmark DB:', e);
  }
}

// Configure le nombre de threads pour le benchmark
export async function setBenchmarkThreadCount(count: number): Promise<void> {
  if (!benchmarkConn) {
    await initBenchmarkDuckDB();
  }
  if (!benchmarkConn) throw new Error("Benchmark DuckDB not initialized");
  await benchmarkConn.query(`PRAGMA threads=${count};`);
}

// Obtient le nombre de threads disponibles pour le benchmark
// Note: Le bundle COI n'a pas duckdb_threads, donc on retourne 0 dans ce cas
export async function getBenchmarkThreadCount(): Promise<number> {
  if (!benchmarkConn) {
    await initBenchmarkDuckDB();
  }
  if (!benchmarkConn) throw new Error("Benchmark DuckDB not initialized");
  
  try {
    // Essayer avec duckdb_types (COI bundle) ou duckdb_threads (autres)
    const result = await benchmarkConn.query("SELECT COUNT(*) as cnt FROM duckdb_types()");
    return result.toArray()[0]?.cnt ?? 0;
  } catch (e) {
    // Si duckdb_types échoue, essayer duckdb_threads
    try {
      const result = await benchmarkConn.query("SELECT COUNT(*) as cnt FROM duckdb_threads();");
      return result.toArray()[0]?.cnt ?? 0;
    } catch (e2) {
      return 0;
    }
  }
}

export async function queryLastPositions(
  date: string,
  bounds: Bounds | null,
  limit = 100000
): Promise<Vessel[]> {
  if (!conn) throw new Error("DuckDB not initialized");

  const qid = ++querySeq;

  const d = new Date(date);
  const year = d.getUTCFullYear();
  const month = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = d.getUTCDate();
  const ts = d.toISOString().slice(0, 19).replace("T", " ");

  let spatialFilter = "";
  let boundsDesc = "none";
  if (bounds) {
    spatialFilter = `
      AND p.lat BETWEEN ${bounds.south} AND ${bounds.north}
      AND p.lon BETWEEN ${bounds.west} AND ${bounds.east}
    `;
    boundsDesc = `${bounds.west.toFixed(1)},${bounds.south.toFixed(1)},${bounds.east.toFixed(1)},${bounds.north.toFixed(1)}`;
  }

  const sql = `
    SELECT DISTINCT ON (p.mmsi)
      p.mmsi, p.lat, p.lon, p.sog, p.cog, p.true_heading,
      p.ts, v.name, v.ship_type, v.destination
    FROM ais.vessels_positions p
    LEFT JOIN ais.vessels v ON v.mmsi = p.mmsi
    WHERE p.year = ${year}
      AND p.month = '${month}'
      AND p.day = ${day}
      AND p.ts BETWEEN TIMESTAMP '${ts}' AND TIMESTAMP '${ts}' + INTERVAL '10 minutes'
      AND p.lat IS NOT NULL
      AND p.lon IS NOT NULL
      ${spatialFilter}
    ORDER BY p.mmsi, p.ts DESC
    LIMIT ${limit}
  `;

  const t0 = performance.now();
  console.log(`[DuckDB] q#${qid} ▶ bounds=[${boundsDesc}] date=${ts}`);

  let rows: any[];
  try {
    const asyncResult = await conn.send(sql);
    rows = [];
    for await (const chunk of asyncResult) {
      rows.push(...chunk);
    }
  } catch (e: any) {
    const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
    console.log(`[DuckDB] q#${qid} ✗ error after ${elapsed}s: ${e.message}`);
    throw e;
  }

  const t1 = performance.now();
  const vessels = rows.map((row: any) => toVessel(row));
  const t2 = performance.now();

  console.log(
    `[DuckDB] q#${qid} ✓ ${vessels.length} rows ` +
    `(query: ${(t1 - t0).toFixed(0)}ms, ` +
    `toArray+map: ${(t2 - t1).toFixed(0)}ms, ` +
    `total: ${(t2 - t0).toFixed(0)}ms)`
  );

  return vessels;
}

export async function queryVesselHistory(
  mmsi: number,
  vesselTs: string | Date,
  daysBack = 3,
): Promise<{ lat: number; lng: number; ts: Date }[]> {
  if (!conn) throw new Error("DuckDB not initialized");

  const end = new Date(vesselTs);
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - daysBack);
  if (start > end) return [];

  const startEpoch = Math.floor(start.getTime() / 1000);
  const endEpoch = Math.floor(end.getTime() / 1000);
  const startDate = start.toISOString().slice(0, 10);
  const endDate = end.toISOString().slice(0, 10);

  const sql = `
    SELECT lat, lon, ts
    FROM ais.vessel_tracks
    WHERE mmsi = ${mmsi}
      AND date >= '${startDate}'
      AND date <= '${endDate}'
      AND ts >= ${startEpoch}
      AND ts <= ${endEpoch}
      AND lat IS NOT NULL
      AND lon IS NOT NULL
    ORDER BY ts ASC
  `;

  const asyncResult = await conn.send(sql);
  const rows: any[] = [];
  for await (const chunk of asyncResult) {
    rows.push(...chunk);
  }
  return rows.map((row: any) => ({
    lat: Number(row.lat) / 1e5,
    lng: Number(row.lon) / 1e5,
    ts: new Date(Number(row.ts) * 1000),
  }));
}

function toVessel(row: any): Vessel {
  return {
    id: Number(row.mmsi),
    name: row.name ?? "Unknown",
    lat: Number(row.lat),
    lng: Number(row.lon),
    heading: Number(row.true_heading ?? row.cog ?? 0),
    speed: Number(row.sog ?? 0),
    shipType: shipTypeAISToCategory(row.ship_type != null ? Number(row.ship_type) : null),
    destination: row.destination ?? undefined,
    ts: row.ts ?? undefined,
  };
}

// Interface pour les résultats de benchmark
export interface BenchmarkResult {
  query: string;
  mode: 'threads' | 'eh';
  times: number[];
  avg: number;
  min: number;
  max: number;
  iterations: number;
}

// Exécute un benchmark sur une requête
export async function benchmarkQuery(
  query: string,
  iterations: number = 5
): Promise<BenchmarkResult> {
  if (!conn) throw new Error("DuckDB not initialized");
  if (!initMode) throw new Error("DuckDB init mode unknown");

  const times: number[] = [];
  
  for (let i = 0; i < iterations; i++) {
    const start = performance.now();
    await conn.query(query);
    const duration = performance.now() - start;
    times.push(duration);
  }

  const avg = times.reduce((a, b) => a + b, 0) / times.length;
  
  return {
    query,
    mode: initMode,
    times,
    avg,
    min: Math.min(...times),
    max: Math.max(...times),
    iterations,
  };
}

// Obtient le nombre de threads disponibles
export async function getThreadCount(): Promise<number> {
  if (!conn) throw new Error("DuckDB not initialized");
  
  try {
    const result = await conn.query("SELECT COUNT(*) as cnt FROM duckdb_threads();");
    return result.toArray()[0]?.cnt ?? 0;
  } catch (e) {
    // En mode EH, duckdb_threads() n'existe pas
    return 0;
  }
}

// Configure le nombre de threads à utiliser
export async function setThreadCount(count: number): Promise<void> {
  if (!conn) throw new Error("DuckDB not initialized");
  await conn.query(`PRAGMA threads=${count};`);
}

// Crée une table de test avec des données générées pour les benchmarks
export async function createBenchmarkTable(rowCount: number = 1000000): Promise<void> {
  if (!conn) throw new Error("DuckDB not initialized");
  
  await conn.query(`
    CREATE OR REPLACE TABLE benchmark_data AS
    SELECT 
      i as id,
      random() * 100 as value1,
      random() * 1000 as value2,
      'type_' || (random() * 10)::integer as category,
      CAST(date '2024-01-01' AS date) + (random() * 365)::integer * INTERVAL 1 day as date_val
    FROM generate_series(1, ${rowCount}) as i
  `);
}

// Supprime la table de benchmark
export async function dropBenchmarkTable(): Promise<void> {
  if (!conn) throw new Error("DuckDB not initialized");
  
  try {
    await conn.query("DROP TABLE IF EXISTS benchmark_data;");
  } catch (e) {
    console.warn('[DuckDB] Error dropping benchmark table:', e);
  }
}
