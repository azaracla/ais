import { useEffect, useState, useRef, useCallback } from "react";
import { initDuckDB, isReady, queryLastPositions, cancelQuery } from "./duckdb";
import type { Vessel, Bounds } from "./types";

const DEBOUNCE_MS = 400;

export function useVessels(
  date: string,
  bounds: Bounds | null
): { vessels: Vessel[]; loading: boolean; error: string | null; ready: boolean } {
  const [vessels, setVessels] = useState<Vessel[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();
  const genRef = useRef(0);

  useEffect(() => {
    initDuckDB()
      .then(() => setReady(true))
      .catch((e) => setError(e.message ?? "Failed to init DuckDB"));
  }, []);

  const fetch = useCallback(async (b: Bounds, d: string) => {
    if (!isReady()) return;

    await cancelQuery(); // cancel any in-flight query
    const generation = ++genRef.current;
    setLoading(true);
    setError(null);

    try {
      const data = await queryLastPositions(d, b);
      if (generation !== genRef.current) return; // stale
      setVessels(data);
    } catch (e: any) {
      if (generation !== genRef.current) return;
      setError(e.message ?? "Query failed");
      setVessels([]);
    } finally {
      if (generation === genRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (!bounds || !ready) return;
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => fetch(bounds, date), DEBOUNCE_MS);
    return () => clearTimeout(timerRef.current);
  }, [bounds, date, ready, fetch]);

  return { vessels, loading, error, ready };
}
