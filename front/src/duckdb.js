import * as duckdb from '@duckdb/duckdb-wasm';
import duckdb_mvp from '@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm?url';
import mvp_worker from '@duckdb/duckdb-wasm/dist/duckdb-browser-mvp.worker.js?url';

let db, conn;

const BUNDLES = {
  mvp: { mainModule: duckdb_mvp, mainWorker: mvp_worker },
};

export async function initDuckDB() {
  if (db) return;
  const bundle = await duckdb.selectBundle(BUNDLES);
  const worker = new Worker(bundle.mainWorker);
  db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  conn = await db.connect();
  await conn.query("LOAD 'httpfs'");
  
  // Try DuckLake first
  try {
    await conn.query("LOAD 'ducklake'");
    await conn.query(`ATTACH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/metadata.ducklake' AS ais (TYPE ducklake, AUTOMATIC_MIGRATION true)`);
    console.log('DuckLake loaded successfully');
  } catch (e) {
    console.warn('DuckLake failed, trying direct parquet scan:', e.message);
    // Fallback: try to read parquet files directly
    // We need to find the right path pattern
    try {
      // Try common patterns
      const patterns = [
        'https://ais-public-prod.s3.gra.io.cloud.ovh.net/**/*.parquet',
        'https://ais-public-prod.s3.gra.io.cloud.ovh.net/silver/**/*.parquet',
        'https://ais-public-prod.s3.gra.io.cloud.ovh.net/data/**/*.parquet',
        'https://ais-public-prod.s3.gra.io.cloud.ovh.net/messages/**/*.parquet'
      ];
      
      for (const pattern of patterns) {
        try {
          await conn.query(`
            CREATE VIEW ais_messages AS 
            SELECT * FROM read_parquet('${pattern}')
          `);
          console.log(`Success with pattern: ${pattern}`);
          break;
        } catch (e2) {
          console.warn(`Pattern failed: ${pattern}`, e2.message);
        }
      }
    } catch (e3) {
      console.error('All patterns failed:', e3);
      throw new Error('Could not load data. DuckLake version mismatch and no parquet files found at common paths.');
    }
  }
}

export async function queryLastPositions(timeRange, options = {}) {
  const { start, end } = timeRange;
  const { limit = 1000000 } = options;
  return conn.query(`
    WITH ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY ts DESC) as rn
      FROM ais_messages
      WHERE ts BETWEEN '${start}' AND '${end}' AND lat IS NOT NULL AND lon IS NOT NULL
    )
    SELECT mmsi, lat, lon, cog, sog, name, imo_number, message_type, ts
    FROM ranked WHERE rn = 1 LIMIT ${limit}
  `);
}

export async function searchShips(query, limit = 10) {
  const num = parseInt(query);
  if (isNaN(num)) {
    const name = query.replace(/'/g, "''");
    return conn.query(`
      SELECT DISTINCT mmsi, name, imo_number, message_type
      FROM ais_messages WHERE name ILIKE '%${name}%' AND name IS NOT NULL
      LIMIT ${limit}
    `);
  }
  return conn.query(`
    SELECT DISTINCT mmsi, name, imo_number, message_type, lat, lon, ts
    FROM ais_messages WHERE mmsi = ${num} OR imo_number = ${num}
    ORDER BY ts DESC LIMIT ${limit}
  `);
}

export async function getTimeRange() {
  const r = await conn.query("SELECT MIN(ts) as min_ts, MAX(ts) as max_ts FROM ais_messages LIMIT 1");
  return { min: r[0]?.min_ts || new Date(0).toISOString().slice(0, 19),
           max: r[0]?.max_ts || new Date().toISOString().slice(0, 19) };
}

export async function getStats() {
  const [c, d, s] = await Promise.all([
    conn.query("SELECT COUNT(*) as total FROM ais_messages LIMIT 1"),
    conn.query("SELECT MIN(ts) as min_ts, MAX(ts) as max_ts FROM ais_messages LIMIT 1"),
    conn.query("SELECT COUNT(DISTINCT mmsi) as unique_ships FROM ais_messages WHERE mmsi IS NOT NULL LIMIT 1")
  ]);
  return { totalMessages: c[0]?.total || 0, minDate: d[0]?.min_ts, maxDate: d[0]?.max_ts, uniqueShips: s[0]?.unique_ships || 0 };
}

export async function queryShipTrack(mmsi, {start, end}, maxPoints = 1000) {
  return conn.query(`
    SELECT lat, lon, cog, sog, ts FROM ais_messages
    WHERE mmsi = ${mmsi} AND ts BETWEEN '${start}' AND '${end}'
      AND lat IS NOT NULL AND lon IS NOT NULL
    ORDER BY ts DESC LIMIT ${maxPoints}
  `);
}
