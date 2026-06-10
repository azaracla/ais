import { useEffect, useState, useRef, useCallback } from "react";
import { initDuckDB, isReady, queryLastPositions, cancelQuery } from "../duckdb";
import type { Vessel, Bounds } from "../types";

const DEBOUNCE_MS = 400;
const BOUNDS_BUFFER = 3;

function expandBounds(bounds: Bounds): Bounds {
  const factor = Math.sqrt(BOUNDS_BUFFER);
  const cx = (bounds.west + bounds.east) / 2;
  const cy = (bounds.south + bounds.north) / 2;
  const hw = (bounds.east - bounds.west) / 2 * factor;
  const hh = (bounds.north - bounds.south) / 2 * factor;
  return {
    west: cx - hw,
    east: cx + hw,
    south: cy - hh,
    north: cy + hh,
  };
}

function isInside(viewport: Bounds, loaded: Bounds): boolean {
  return viewport.west >= loaded.west
    && viewport.east <= loaded.east
    && viewport.south >= loaded.south
    && viewport.north <= loaded.north;
}

export function useVessels(
  date: string,
  bounds: Bounds | null
): { vessels: Vessel[]; loading: boolean; error: string | null; ready: boolean } {
  const [vessels, setVessels] = useState<Vessel[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const genRef = useRef(0);
  const loadedBoundsRef = useRef<Bounds | null>(null);
  const accumulatedRef = useRef<Map<number, Vessel>>(new Map());

  useEffect(() => {
    initDuckDB()
      .then(() => setReady(true))
      .catch((e) => setError(e.message ?? "Failed to init DuckDB"));
  }, []);

  useEffect(() => {
    loadedBoundsRef.current = null;
    accumulatedRef.current = new Map();
    setVessels([]);
  }, [date]);

  const fetch = useCallback(async (b: Bounds, d: string) => {
    if (!isReady()) return;

    await cancelQuery();
    const generation = ++genRef.current;
    setLoading(true);
    setError(null);

    const expanded = expandBounds(b);

    try {
      const data = await queryLastPositions(d, expanded);
      if (generation !== genRef.current) return;
      loadedBoundsRef.current = expanded;
      for (const v of data) {
        accumulatedRef.current.set(v.id, v);
      }
      setVessels(Array.from(accumulatedRef.current.values()));
    } catch (e: any) {
      if (generation !== genRef.current) return;
      setError(e.message ?? "Query failed");
    } finally {
      if (generation === genRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (!bounds || !ready) return;
    if (loadedBoundsRef.current && isInside(bounds, loadedBoundsRef.current)) return;
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => fetch(bounds, date), DEBOUNCE_MS);
    return () => clearTimeout(timerRef.current);
  }, [bounds, date, ready, fetch]);

  return { vessels, loading, error, ready };
}
