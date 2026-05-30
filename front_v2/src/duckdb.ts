import * as duckdb from "@duckdb/duckdb-wasm";
import duckdb_wasm_eh from "@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url";
import duckdb_wasm_mvp from "@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm?url";
import DuckDBWorkerEH from "@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?worker";
import DuckDBWorkerMVP from "@duckdb/duckdb-wasm/dist/duckdb-browser-mvp.worker.js?worker";
import type { Vessel, Bounds } from "./types";
import { shipTypeAISToCategory } from "./types";

const BUNDLES = {
  mvp: {
    mainModule: duckdb_wasm_mvp,
    mainWorker: DuckDBWorkerMVP,
  },
  eh: {
    mainModule: duckdb_wasm_eh,
    mainWorker: DuckDBWorkerEH,
  },
};

let db: duckdb.AsyncDuckDB | null = null;
let conn: duckdb.AsyncDuckDBConnection | null = null;
let initPromise: Promise<void> | null = null;
let querySeq = 0;

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

export async function initDuckDB(): Promise<void> {
  if (initPromise) return initPromise;

  initPromise = (async () => {
    const bundle = await duckdb.selectBundle(BUNDLES);
    const worker = new bundle.mainWorker();
    db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
    await db.instantiate(bundle.mainModule, "?modulePath=");
    conn = await db.connect();

    await conn.query("SET enable_object_cache=true;");
    await conn.query(
      "ATTACH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/ais.ducklake' AS ais (TYPE ducklake)"
    );
    const r = await conn.query("SELECT COUNT(*) as cnt FROM ais.vessels_positions LIMIT 1;");
    const cnt = r.toArray()[0]?.cnt ?? 0;
    console.log("[DuckDB] Initialized. Records:", cnt);
  })();

  return initPromise;
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
    WITH ranked AS (
      SELECT
        p.mmsi, p.lat, p.lon, p.sog, p.cog, p.true_heading, p.ts,
        ROW_NUMBER() OVER (PARTITION BY p.mmsi ORDER BY p.ts DESC) as rn
      FROM ais.vessels_positions p
      WHERE p.year = ${year}
        AND p.month = '${month}'
        AND p.day = ${day}
        AND p.ts BETWEEN TIMESTAMP '${ts}' AND TIMESTAMP '${ts}' + INTERVAL '10 minutes'
        AND p.lat IS NOT NULL
        AND p.lon IS NOT NULL
        ${spatialFilter}
    )
    SELECT
      p.mmsi, p.lat, p.lon, p.sog, p.cog, p.true_heading,
      v.name, v.ship_type, v.destination
    FROM ranked p
    LEFT JOIN ais.vessels v ON v.mmsi = p.mmsi
    WHERE p.rn = 1
    LIMIT ${limit}
  `;

  const t0 = performance.now();
  console.log(`[DuckDB] q#${qid} ▶ bounds=[${boundsDesc}] date=${ts}`);

  let result;
  try {
    result = await conn.query(sql);
  } catch (e: any) {
    const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
    console.log(`[DuckDB] q#${qid} ✗ error after ${elapsed}s: ${e.message}`);
    throw e;
  }

  const t1 = performance.now();
  const rows = result.toArray();
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
  };
}
