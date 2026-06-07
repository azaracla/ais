import { useEffect, useState, useRef, useCallback } from "react";
import { getAllVessels } from "./duckdb";
import type { VesselSummary } from "./types";

export function useVesselSearch() {
  const [cache, setCache] = useState<Map<number, VesselSummary> | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const allVessels = useRef<VesselSummary[]>([]);
  const loadedRef = useRef(false);

  useEffect(() => {
    if (loadedRef.current) return;
    loadedRef.current = true;
    getAllVessels()
      .then((c) => {
        setCache(c);
        allVessels.current = Array.from(c.values());
        setReady(true);
      })
      .catch((e) => setError(e.message ?? "Failed to load vessel index"));
  }, []);

  const search = useCallback(
    (query: string, limit = 15): VesselSummary[] => {
      if (!query.trim() || allVessels.current.length === 0) return [];
      const q = query.toLowerCase();
      const results: VesselSummary[] = [];
      for (const v of allVessels.current) {
        if (
          v.name.toLowerCase().includes(q) ||
          String(v.mmsi).includes(q)
        ) {
          results.push(v);
          if (results.length >= limit) break;
        }
      }
      return results;
    },
    [],
  );

  const find = useCallback(
    (mmsi: number): VesselSummary | undefined => cache?.get(mmsi),
    [cache],
  );

  return { ready, error, search, find, cache };
}
