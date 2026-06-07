import * as duckdb from "@duckdb/duckdb-wasm";
import duckdb_wasm_eh from "@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url";
import DuckDBWorkerEH from "@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?worker";
import type { Vessel, VesselDetail, VesselSummary, Bounds } from "./types";
import { shipTypeAISToCategory } from "./types";

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
    const worker = new DuckDBWorkerEH();
    db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
    await db.instantiate(duckdb_wasm_eh, "?modulePath=");

    await db.open({
      filesystem: {
        allowFullHTTPReads: false,
        reliableHeadRequests: true,
        forceFullHTTPReads: false,
      },
    });

    conn = await db.connect();

    await conn.query("SET enable_object_cache=true;");
    await conn.query(
      "ATTACH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake' AS ais (TYPE ducklake, DATA_PATH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake.files/', OVERRIDE_DATA_PATH true)"
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
): Promise<{ lat: number; lng: number; ts: Date; heading: number | null }[]> {
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
    SELECT lat, lon, ts, heading
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
    heading: row.heading != null ? Number(row.heading) : null,
  }));
}

let vesselCache: Map<number, VesselSummary> | null = null;
let vesselCachePromise: Promise<Map<number, VesselSummary>> | null = null;

export async function getAllVessels(): Promise<Map<number, VesselSummary>> {
  if (vesselCache) return vesselCache;
  if (vesselCachePromise) return vesselCachePromise;

  vesselCachePromise = (async () => {
    if (!conn) throw new Error("DuckDB not initialized");
    const sql = `SELECT mmsi, name, ship_type FROM ais.vessels WHERE name IS NOT NULL`;
    const t0 = performance.now();
    const result = await conn.send(sql);
    const rows: any[] = [];
    for await (const chunk of result) {
      rows.push(...chunk);
    }
    const cache = new Map<number, VesselSummary>();
    for (const row of rows) {
      cache.set(Number(row.mmsi), {
        mmsi: Number(row.mmsi),
        name: row.name ?? "Unknown",
        shipType: shipTypeAISToCategory(row.ship_type != null ? Number(row.ship_type) : null),
      });
    }
    vesselCache = cache;
    const t1 = performance.now();
    console.log(`[DuckDB] Vessel cache loaded: ${cache.size} vessels in ${(t1 - t0).toFixed(0)}ms`);
    return cache;
  })();

  return vesselCachePromise;
}

export async function getVesselDetail(mmsi: number): Promise<VesselDetail | null> {
  if (!conn) throw new Error("DuckDB not initialized");

  const sql = `
    SELECT v.mmsi, v.name, v.call_sign, v.imo_number, v.ship_type,
           v.length, v.width, v.destination, v.last_seen_static,
           p.lat, p.lon, p.sog, p.cog, p.true_heading, p.ts, p.navigational_status
    FROM ais.vessels v
    LEFT JOIN (
      SELECT DISTINCT ON (mmsi) mmsi, lat, lon, sog, cog, true_heading, ts, navigational_status
      FROM ais.vessels_positions
      WHERE mmsi = ${mmsi}
      ORDER BY mmsi, ts DESC
      LIMIT 1
    ) p ON v.mmsi = p.mmsi
    WHERE v.mmsi = ${mmsi}
  `;

  const result = await conn.send(sql);
  const rows: any[] = [];
  for await (const chunk of result) {
    rows.push(...chunk);
  }
  if (rows.length === 0) return null;
  const row = rows[0];

  return {
    id: Number(row.mmsi),
    name: row.name ?? "Unknown",
    lat: Number(row.lat ?? 0),
    lng: Number(row.lon ?? 0),
    heading: Number(row.true_heading ?? row.cog ?? 0),
    speed: Number(row.sog ?? 0),
    shipType: shipTypeAISToCategory(row.ship_type != null ? Number(row.ship_type) : null),
    destination: row.destination ?? undefined,
    ts: row.ts ?? undefined,
    imo: row.imo_number != null ? Number(row.imo_number) : undefined,
    callSign: row.call_sign ?? undefined,
    length: row.length != null ? Number(row.length) : undefined,
    width: row.width != null ? Number(row.width) : undefined,
    navStatus: row.navigational_status != null ? Number(row.navigational_status) : undefined,
  };
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
