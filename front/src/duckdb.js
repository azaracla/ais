import * as duckdb from '@duckdb/duckdb-wasm';

// Import WASM bundles
import duckdb_wasm_eh from '@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url';
import duckdb_wasm_mvp from '@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm?url';

// Import Workers using Vite's ?worker syntax
import DuckDBWorkerEH from '@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?worker';
import DuckDBWorkerMVP from '@duckdb/duckdb-wasm/dist/duckdb-browser-mvp.worker.js?worker';

let db, conn;

const BUNDLES = {
    mvp: {
        mainModule: duckdb_wasm_mvp,
        mainWorker: DuckDBWorkerMVP,
    },
    eh: {
        mainModule: duckdb_wasm_eh,
        mainWorker: DuckDBWorkerEH,
    }
};

export async function initDuckDB() {
  if (db) return;

  // We use the 'eh' bundle because the 'ducklake' extension is not compatible with WASM pthreads/shared memory.
  const bundle = await duckdb.selectBundle(BUNDLES);

  // Instantiate the worker using the constructor provided by Vite
  const worker = new bundle.mainWorker();
  db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
  await db.instantiate(bundle.mainModule);
  conn = await db.connect();
  
  await conn.query("SET enable_object_cache=true;");
  await conn.query("CREATE VIEW vessels AS SELECT * FROM read_parquet('https://ais-public-prod.s3.gra.io.cloud.ovh.net/gold/vessels.parquet');")
  // Multi-threading is disabled here to maintain compatibility with the ducklake extension and external map tiles
  await conn.query(`ATTACH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/ais.ducklake' AS ais (TYPE ducklake)`);
  const count = await conn.query("SELECT COUNT(*) as cnt FROM ais.messages LIMIT 1;");
  console.log('DuckDB initialized. Valid records:', count.toArray()[0]?.cnt ?? 0);
}

export async function queryLastPositions(timeRange, options = {}) {
  const { start, end } = timeRange;
  const { limit = 100000, bounds = null } = options;

  try {
    const dStart = new Date(start);
    const dEnd = new Date(end);

    // Extraction des partitions pour le Pruning (accélère DuckLake x10)
    const years = [...new Set([dStart.getUTCFullYear(), dEnd.getUTCFullYear()])];
    const months = [...new Set([
      String(dStart.getUTCMonth() + 1).padStart(2, '0'), 
      String(dEnd.getUTCMonth() + 1).padStart(2, '0')
    ])].map(m => `'${m}'`);
    const days = [...new Set([dStart.getUTCDate(), dEnd.getUTCDate()])];

    let spatialFilter = "";
    if (bounds) {
      spatialFilter = `
        AND lat BETWEEN ${bounds.south} AND ${bounds.north}
        AND lon BETWEEN ${bounds.west} AND ${bounds.east}
      `;
    }

    const result = await conn.query(`
      WITH ranked AS (
        SELECT 
          mmsi, lat, lon, cog, sog, name, imo_number, message_type, ship_type, ts,
          ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY ts DESC) as rn
        FROM ais.messages
        WHERE year IN (${years.join(',')})
          AND month IN (${months.join(',')})
          AND day IN (${days.join(',')})
          AND ts BETWEEN TIMESTAMP '${start}' AND TIMESTAMP '${end}' 
          AND lat IS NOT NULL 
          AND lon IS NOT NULL
          ${spatialFilter}
      )
      SELECT mmsi, lat, lon, cog, sog, v.name, v.imo_number, message_type, v.ship_type, ts
      FROM ranked
      LEFT JOIN vessels v USING (mmsi)
      WHERE rn = 1
      LIMIT ${limit}
    `);
    
    return result.toArray();
  } catch (err) {
    console.error("Erreur lors de la requête DuckLake:", err);
    return [];
  }
}

export async function searchShips(query, limit = 10) {
  const num = parseInt(query);
  if (isNaN(num)) {
    const name = query.replace(/'/g, "''");
    const result = await conn.query(`
      SELECT DISTINCT mmsi, name, imo_number, message_type, ship_type
      FROM ais.messages WHERE name ILIKE '%${name}%' AND name IS NOT NULL
      LIMIT ${limit}
    `);
    return result.toArray();
  }
  const result = await conn.query(`
    SELECT DISTINCT mmsi, name, imo_number, message_type, ship_type, lat, lon, ts
    FROM ais.messages WHERE mmsi = ${num} OR imo_number = ${num}
    ORDER BY ts DESC LIMIT ${limit}
  `);
  return result.toArray();
}

export async function getTimeRange() {
  const r = await conn.query("SELECT MIN(ts) as min_ts, MAX(ts) as max_ts FROM ais.messages LIMIT 1");
  const arr = r.toArray();
  
  // Conversion explicite en ISO string car DuckDB-Wasm renvoie parfois des nombres
  const toISO = (val) => {
    if (!val) return null;
    return (val instanceof Date ? val : new Date(Number(val))).toISOString().slice(0, 19);
  };

  return { 
    min: toISO(arr[0]?.min_ts) || new Date(Date.now() - 7*24*3600*1000).toISOString().slice(0, 19),
    max: toISO(arr[0]?.max_ts) || new Date().toISOString().slice(0, 19) 
  };
}

export async function getStats() {
  const [c, d, s] = await Promise.all([
    conn.query("SELECT COUNT(*) as total FROM ais.messages LIMIT 1"),
    conn.query("SELECT MIN(ts) as min_ts, MAX(ts) as max_ts FROM ais.messages LIMIT 1"),
    conn.query("SELECT COUNT(DISTINCT mmsi) as unique_ships FROM ais.messages WHERE mmsi IS NOT NULL LIMIT 1")
  ]);
  
  const toISO = (val) => {
    if (!val) return null;
    return (val instanceof Date ? val : new Date(Number(val))).toISOString().slice(0, 19);
  };

  return { 
    totalMessages: c.toArray()[0]?.total || 0, 
    minDate: toISO(d.toArray()[0]?.min_ts), 
    maxDate: toISO(d.toArray()[0]?.max_ts), 
    uniqueShips: s.toArray()[0]?.unique_ships || 0 
  };
}

export async function queryShipTrack(mmsi, {start, end}, maxPoints = 1000) {
  const result = await conn.query(`
    SELECT lat, lon, cog, sog, ts FROM ais.messages
    WHERE mmsi = ${mmsi} AND ts BETWEEN '${start}' AND '${end}'
      AND lat IS NOT NULL AND lon IS NOT NULL
    ORDER BY ts DESC LIMIT ${maxPoints}
  `);
  return result.toArray();
}
