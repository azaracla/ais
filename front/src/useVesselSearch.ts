import { useState, useRef, useCallback } from "react";
import { searchVessels } from "./duckdb";
import type { VesselSummary } from "./types";

export function useVesselSearch() {
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const genRef = useRef(0);

  const search = useCallback(
    async (query: string, limit = 15): Promise<VesselSummary[]> => {
      if (!query.trim()) return [];
      const gen = ++genRef.current;
      setLoading(true);
      setError(null);
      try {
        const results = await searchVessels(query, limit);
        if (gen !== genRef.current) return [];
        return results;
      } catch (e: any) {
        if (gen !== genRef.current) return [];
        setError(e.message ?? "Search failed");
        return [];
      } finally {
        if (gen === genRef.current) setLoading(false);
      }
    },
    [],
  );

  return { error, loading, search };
}
