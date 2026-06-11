import type { Vessel, VesselSummary, Bounds, WakePoint, PortCongestion, PortCall } from "./types";
import { shipTypeAISToCategory } from "./types";

// Lazy loaded DuckDB module
let duckdbModule: typeof import("@duckdb/duckdb-wasm") | null = null;
let duckdb_wasm_eh: string | null = null;
let DuckDBWorkerEH: any = null;

// SQL parameter sanitization helpers
function sanitizeNumber(n: unknown): number {
  const num = Number(n);
  if (!Number.isFinite(num)) throw new Error(`Invalid number: ${n}`);
  return num;
}

function sanitizeString(s: unknown): string {
  const str = String(s);
  // Escape single quotes for SQL
  return str.replace(/'/g, "''");
}

function sanitizeDate(dateStr: unknown): string {
  const str = String(dateStr);
  // Validate ISO date format YYYY-MM-DD
  if (!/^\d{4}-\d{2}-\d{2}$/.test(str)) {
    throw new Error(`Invalid date format: ${str}`);
  }
  return str;
}

function sanitizeTimestamp(ts: unknown): string {
  const str = String(ts);
  // Validate ISO timestamp format
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$/.test(str)) {
    throw new Error(`Invalid timestamp format: ${str}`);
  }
  return str;
}

function sanitizeBounds(b: Bounds | null): Bounds | null {
  if (!b) return null;
  return {
    west: sanitizeNumber(b.west),
    east: sanitizeNumber(b.east),
    south: sanitizeNumber(b.south),
    north: sanitizeNumber(b.north),
  };
}

let db: any | null = null;
let conn: any | null = null;
let portConn: any | null = null;
let initPromise: Promise<void> | null = null;
let duckDbReady = false;
let querySeq = 0;

// Cache LRU pour les requêtes
interface CacheEntry {
  key: string;
  data: any;
  timestamp: number;
}

const queryCache = new Map<string, CacheEntry>();
const CACHE_TTL = 5 * 60 * 1000; // 5 minutes
const MAX_CACHE_SIZE = 100;

function generateCacheKey(sql: string, params?: Record<string, unknown>): string {
  return sql + (params ? JSON.stringify(params) : "");
}

function getCached<T>(key: string): T | null {
  const entry = queryCache.get(key);
  if (!entry) return null;
  if (Date.now() - entry.timestamp > CACHE_TTL) {
    queryCache.delete(key);
    return null;
  }
  return entry.data as T;
}

function setCached<T>(key: string, data: T): void {
  queryCache.set(key, { key, data, timestamp: Date.now() });
  // Limiter la taille du cache (LRU)
  if (queryCache.size > MAX_CACHE_SIZE) {
    const firstKey = queryCache.keys().next().value;
    if (firstKey) queryCache.delete(firstKey);
  }
}

export function cancelQuery(): Promise<boolean> {
  const result = conn?.cancelSent() ?? Promise.resolve(false);
  result.then((cancelled: boolean) => {
    if (cancelled) console.log(`[DuckDB] ⏹ q#${querySeq} cancelled`);
  });
  return result;
}

export function isReady() {
  return duckDbReady;
}

export async function initDuckDB(): Promise<void> {
  if (initPromise) return initPromise;

  initPromise = (async () => {
    // Lazy load DuckDB module and assets
    if (!duckdbModule) {
      const [module, wasmUrl, workerModule] = await Promise.all([
        import("@duckdb/duckdb-wasm"),
        import("@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url").then(m => m.default),
        import("@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?worker").then(m => m.default)
      ]);
      duckdbModule = module;
      duckdb_wasm_eh = wasmUrl;
      DuckDBWorkerEH = workerModule;
    }
    
    if (!duckdbModule || !duckdb_wasm_eh || !DuckDBWorkerEH) {
      throw new Error("Failed to load DuckDB module or assets");
    }
    
    const worker = new DuckDBWorkerEH();
    db = new duckdbModule.AsyncDuckDB(new duckdbModule.ConsoleLogger(), worker);
    await db.instantiate(duckdb_wasm_eh, "?modulePath=");

    await db.open({
      filesystem: {
        allowFullHTTPReads: true,
        reliableHeadRequests: true,
        forceFullHTTPReads: false,
      },
    });

    conn = await db.connect();
    portConn = await db.connect();

    await conn.query("SET enable_object_cache=false;");
    await conn.query("SET enable_http_metadata_cache=false;");
    await conn.query(
      "ATTACH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake' AS ais (TYPE ducklake, DATA_PATH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake.files/', OVERRIDE_DATA_PATH true)"
    );
    const r = await conn.query("SELECT COUNT(*) as cnt FROM ais.vessels_positions LIMIT 1;");
    const cnt = r.toArray()[0]?.cnt ?? 0;
    console.log("[DuckDB] Initialized. Records:", cnt);
    duckDbReady = true;
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
  
  // Generate cache key
  const cacheKey = generateCacheKey(
    `lastPositions:${date}:${bounds ? JSON.stringify(bounds) : "null"}:${limit}`
  );
  const cached = getCached<Vessel[]>(cacheKey);
  if (cached) {
    console.log(`[Cache] HIT for ${cacheKey}`);
    return cached;
  }

  const d = new Date(date);
  const year = sanitizeNumber(d.getUTCFullYear());
  const month = sanitizeString(String(d.getUTCMonth() + 1).padStart(2, "0"));
  const day = sanitizeNumber(d.getUTCDate());
  const ts = sanitizeTimestamp(d.toISOString());
  const validatedLimit = sanitizeNumber(limit);

  const validatedBounds = sanitizeBounds(bounds);

  let spatialFilter = "";
  let boundsDesc = "none";
  if (validatedBounds) {
    spatialFilter = `
      AND p.lat BETWEEN ${validatedBounds.south} AND ${validatedBounds.north}
      AND p.lon BETWEEN ${validatedBounds.west} AND ${validatedBounds.east}
    `;
    boundsDesc = `${validatedBounds.west.toFixed(1)},${validatedBounds.south.toFixed(1)},${validatedBounds.east.toFixed(1)},${validatedBounds.north.toFixed(1)}`;
  }

  const sql = `
    SELECT DISTINCT ON (p.mmsi)
      p.mmsi, p.lat, p.lon, p.sog, p.cog, p.true_heading, p.navigational_status,
      p.ts, v.name, v.ship_type, v.destination,
      v.imo_number, v.call_sign, v.length, v.width, v.last_seen_static
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
    LIMIT ${validatedLimit}
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

  // Cache the result
  setCached(cacheKey, vessels);

  return vessels;
}

export async function queryVesselHistory(
  mmsi: number,
  vesselTs: string | Date,
  daysBack = 3,
  limit = 1000,
): Promise<{ lat: number; lng: number; ts: Date; heading: number | null }[]> {
  if (!conn) throw new Error("DuckDB not initialized");

  // Generate cache key
  const cacheKey = generateCacheKey(
    `vesselHistory:${mmsi}:${vesselTs}:${daysBack}:${limit}`
  );
  const cached = getCached<{ lat: number; lng: number; ts: Date; heading: number | null }[]>(cacheKey);
  if (cached) {
    console.log(`[Cache] HIT for ${cacheKey}`);
    return cached;
  }

  const validatedMmsi = sanitizeNumber(mmsi);
  const validatedDaysBack = sanitizeNumber(daysBack);
  const validatedLimit = sanitizeNumber(limit);

  const end = new Date(vesselTs);
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - validatedDaysBack);
  if (start > end) return [];

  const startEpoch = sanitizeNumber(Math.floor(start.getTime() / 1000));
  const endEpoch = sanitizeNumber(Math.floor(end.getTime() / 1000));
  const startDate = sanitizeDate(start.toISOString().slice(0, 10));
  const endDate = sanitizeDate(end.toISOString().slice(0, 10));

  const sql = `
    SELECT lat, lon, ts, heading
    FROM ais.vessel_tracks
    WHERE mmsi = ${validatedMmsi}
      AND date >= '${startDate}'
      AND date <= '${endDate}'
      AND ts >= ${startEpoch}
      AND ts <= ${endEpoch}
      AND lat IS NOT NULL
      AND lon IS NOT NULL
    ORDER BY ts ASC
    LIMIT ${validatedLimit}
  `;

  const asyncResult = await conn.send(sql);
  const rows: any[] = [];
  for await (const chunk of asyncResult) {
    rows.push(...chunk);
  }
  const result = rows.map((row: any) => ({
    lat: Number(row.lat) / 1e5,
    lng: Number(row.lon) / 1e5,
    ts: new Date(Number(row.ts) * 1000),
    heading: row.heading != null ? Number(row.heading) : null,
  })) as { lat: number; lng: number; ts: Date; heading: number | null }[];
  
  // Cache the result
  setCached<{ lat: number; lng: number; ts: Date; heading: number | null }[]>(cacheKey, result);
  
  return result;
}

export async function queryPositionsAtTime(
  date: string,
  timestamp: string,
  bounds: Bounds | null,
  limit = 100000,
): Promise<Vessel[]> {
  if (!conn) throw new Error("DuckDB not initialized");

  // Generate cache key
  const cacheKey = generateCacheKey(
    `positionsAtTime:${date}:${timestamp}:${bounds ? JSON.stringify(bounds) : "null"}:${limit}`
  );
  const cached = getCached<Vessel[]>(cacheKey);
  if (cached) {
    console.log(`[Cache] HIT for ${cacheKey}`);
    return cached;
  }

  const d = new Date(date);
  const year = sanitizeNumber(d.getUTCFullYear());
  const month = sanitizeString(String(d.getUTCMonth() + 1).padStart(2, "0"));
  const day = sanitizeNumber(d.getUTCDate());
  const ts = sanitizeTimestamp(new Date(timestamp).toISOString());
  const validatedLimit = sanitizeNumber(limit);

  const validatedBounds = sanitizeBounds(bounds);

  let spatialFilter = "";
  if (validatedBounds) {
    spatialFilter = `
      AND p.lat BETWEEN ${validatedBounds.south} AND ${validatedBounds.north}
      AND p.lon BETWEEN ${validatedBounds.west} AND ${validatedBounds.east}
    `;
  }

  const sql = `
    SELECT DISTINCT ON (p.mmsi)
      p.mmsi, p.lat, p.lon, p.sog, p.cog, p.true_heading, p.navigational_status,
      p.ts, v.name, v.ship_type, v.destination,
      v.imo_number, v.call_sign, v.length, v.width, v.last_seen_static
    FROM ais.vessels_positions p
    LEFT JOIN ais.vessels v ON v.mmsi = p.mmsi
    WHERE p.year = ${year}
      AND p.month = '${month}'
      AND p.day = ${day}
      AND p.ts BETWEEN TIMESTAMP '${ts}' - INTERVAL '15 minutes' AND TIMESTAMP '${ts}' + INTERVAL '5 minutes'
      AND p.lat IS NOT NULL
      AND p.lon IS NOT NULL
      ${spatialFilter}
    ORDER BY p.mmsi, ABS(EPOCH(CAST(p.ts AS TIMESTAMP) - TIMESTAMP '${ts}')) ASC
    LIMIT ${validatedLimit}
  `;

  const asyncResult = await conn.send(sql);
  const rows: any[] = [];
  for await (const chunk of asyncResult) {
    rows.push(...chunk);
  }
  const result = rows.map((row: any) => toVessel(row));
  
  // Cache the result
  setCached(cacheKey, result);
  
  return result;
}

export async function queryVesselWake(
  mmsis: number[],
  startTime: string,
  endTime: string,
): Promise<Map<number, WakePoint[]>> {
  if (!conn || mmsis.length === 0) return new Map();

  // Generate cache key
  const cacheKey = generateCacheKey(
    `vesselWake:${JSON.stringify(mmsis.sort())}:${startTime}:${endTime}`
  );
  const cached = getCached<Map<number, WakePoint[]>>(cacheKey);
  if (cached) {
    console.log(`[Cache] HIT for ${cacheKey}`);
    return cached;
  }

  const validatedMmsis = mmsis.map(sanitizeNumber);
  const startTs = sanitizeTimestamp(new Date(startTime).toISOString());
  const endTs = sanitizeTimestamp(new Date(endTime).toISOString());

  const mmsiList = validatedMmsis.join(",");

  const sql = `
    SELECT mmsi, lat, lon, ts
    FROM ais.vessels_positions
    WHERE mmsi IN (${mmsiList})
      AND ts BETWEEN TIMESTAMP '${startTs}' AND TIMESTAMP '${endTs}'
      AND lat IS NOT NULL
      AND lon IS NOT NULL
    ORDER BY mmsi, ts ASC
    LIMIT 100000
  `;

  const asyncResult = await conn.send(sql);
  const map = new Map<number, WakePoint[]>();
  for await (const chunk of asyncResult) {
    for (const row of chunk) {
      const mmsi = Number(row.mmsi);
      if (!map.has(mmsi)) map.set(mmsi, []);
      map.get(mmsi)!.push({
        lat: Number(row.lat),
        lng: Number(row.lon),
        ts: typeof row.ts === 'string' ? row.ts : String(row.ts),
      });
    }
  }
  
  // Cache the result
  setCached(cacheKey, map);
  
  return map;
}

export async function searchVessels(query: string, limit = 15): Promise<VesselSummary[]> {
  if (!duckDbReady) await initDuckDB();
  if (!conn) throw new Error("DuckDB not initialized");
  if (!query.trim()) return [];

  // Generate cache key
  const cacheKey = generateCacheKey(
    `searchVessels:${query}:${limit}`
  );
  const cached = getCached<VesselSummary[]>(cacheKey);
  if (cached) {
    console.log(`[Cache] HIT for ${cacheKey}`);
    return cached;
  }

  const validatedQuery = sanitizeString(query);
  const validatedLimit = sanitizeNumber(limit);
  const sql = `
    SELECT mmsi, name, ship_type
    FROM ais.vessels
    WHERE name ILIKE '%${validatedQuery}%'
       OR CAST(mmsi AS VARCHAR) LIKE '%${validatedQuery}%'
    ORDER BY name
    LIMIT ${validatedLimit}
  `;

  const result = await conn.send(sql);
  const rows: any[] = [];
  for await (const chunk of result) {
    rows.push(...chunk);
  }
  const vessels = rows.map((row: any) => ({
    mmsi: Number(row.mmsi),
    name: row.name ?? "Unknown",
    shipType: shipTypeAISToCategory(row.ship_type != null ? Number(row.ship_type) : null),
  }));
  
  // Cache the result
  setCached(cacheKey, vessels);
  
  return vessels;
}

export async function queryPortCongestion(date: string): Promise<PortCongestion[]> {
  if (!portConn) throw new Error("DuckDB not initialized");

  // Generate cache key
  const cacheKey = generateCacheKey(
    `portCongestion:${date}`
  );
  const cached = getCached<PortCongestion[]>(cacheKey);
  if (cached) {
    console.log(`[Cache] HIT for ${cacheKey}`);
    return cached;
  }

  const validatedDate = sanitizeDate(date.slice(0, 10));
  const base = "https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake.files/gold";
  const sql = `
    SELECT DISTINCT ON (pcg.port_lo_code)
      pcg.port_lo_code,
      pc.port_name,
      pc.port_lat::DOUBLE AS port_lat,
      pc.port_lon::DOUBLE AS port_lon,
      pcg.hour::VARCHAR AS hour,
      pcg.vessels_in_port::INTEGER AS vessels_in_port,
      pcg.arrivals::INTEGER AS arrivals,
      pcg.departures::INTEGER AS departures
    FROM read_parquet('${base}/port_congestion/port_congestion.parquet') pcg
    JOIN (
      SELECT DISTINCT port_lo_code, port_name, port_lat, port_lon
      FROM read_parquet('${base}/port_calls/port_calls.parquet')
      WHERE port_lat IS NOT NULL AND port_lon IS NOT NULL
    ) pc ON pc.port_lo_code = pcg.port_lo_code
    WHERE pcg.date = '${validatedDate}'
    ORDER BY pcg.port_lo_code, pcg.hour DESC
  `;

  const asyncResult = await portConn.send(sql);
  const rows: any[] = [];
  for await (const chunk of asyncResult) {
    rows.push(...chunk);
  }
  const result = rows as PortCongestion[];
  
  // Cache the result
  setCached(cacheKey, result);
  
  return result;
}

export async function queryPortCalls(
  portLoCode: string,
  date?: string,
  limit = 50,
): Promise<PortCall[]> {
  if (!portConn) throw new Error("DuckDB not initialized");

  // Generate cache key
  const cacheKey = generateCacheKey(
    `portCalls:${portLoCode}:${date ?? ""}:${limit}`
  );
  const cached = getCached<PortCall[]>(cacheKey);
  if (cached) {
    console.log(`[Cache] HIT for ${cacheKey}`);
    return cached;
  }

  const base = "https://ais-public-prod.s3.gra.io.cloud.ovh.net/v3/ais.ducklake.files/gold";
  const validatedPortLoCode = sanitizeString(portLoCode);
  const validatedLimit = sanitizeNumber(limit);
  
  let filter = `port_lo_code = '${validatedPortLoCode}'`;
  if (date) {
    const validatedDate = sanitizeDate(date);
    filter += ` AND arrival_date = '${validatedDate}'`;
  }

  const sql = `
    SELECT mmsi, port_lo_code, port_name, port_lat::DOUBLE AS port_lat, port_lon::DOUBLE AS port_lon,
           arrival_ts::VARCHAR AS arrival_ts,
           arrival_lat::DOUBLE AS arrival_lat,
           arrival_lon::DOUBLE AS arrival_lon,
           departure_ts::VARCHAR AS departure_ts,
           departure_lat::DOUBLE AS departure_lat,
           departure_lon::DOUBLE AS departure_lon,
           destination_clean, detection_method, arrival_date::VARCHAR AS arrival_date
    FROM read_parquet('${base}/port_calls/port_calls.parquet')
    WHERE ${filter}
    ORDER BY arrival_ts DESC
    LIMIT ${validatedLimit}
  `;

  const asyncResult = await portConn.send(sql);
  const rows: any[] = [];
  for await (const chunk of asyncResult) {
    rows.push(...chunk);
  }
  const result = rows as PortCall[];
  
  // Cache the result
  setCached(cacheKey, result);
  
  return result;
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
    imo: row.imo_number != null ? Number(row.imo_number) : undefined,
    callSign: row.call_sign ?? undefined,
    length: row.length != null && isFinite(Number(row.length)) ? Number(row.length) : undefined,
    width: row.width != null && isFinite(Number(row.width)) ? Number(row.width) : undefined,
    navStatus: row.navigational_status != null ? Number(row.navigational_status) : undefined,
    lastSeenStatic: row.last_seen_static ?? undefined,
  };
}
