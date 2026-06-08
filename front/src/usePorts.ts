import { useEffect, useState, useRef } from "react";
import { initDuckDB, queryPortCongestion } from "./duckdb";
import type { PortCongestion } from "./types";

export function usePorts(date: string): {
  ports: PortCongestion[];
  loading: boolean;
  error: string | null;
} {
  const [ports, setPorts] = useState<PortCongestion[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const genRef = useRef(0);

  const [ready, setReady] = useState(false);

  useEffect(() => {
    initDuckDB()
      .then(() => setReady(true))
      .catch((e) => setError(e.message ?? "Failed to init DuckDB"));
  }, []);

  useEffect(() => {
    if (!ready || !date) return;

    const gen = ++genRef.current;
    setLoading(true);
    setError(null);

    queryPortCongestion(date)
      .then((data) => {
        if (gen !== genRef.current) return;
        console.log(`[Ports] Loaded ${data.length} ports for ${date}`);
        setPorts(data);
      })
      .catch((e) => {
        if (gen !== genRef.current) return;
        setError(e.message ?? "Port congestion query failed");
      })
      .finally(() => {
        if (gen === genRef.current) setLoading(false);
      });
  }, [date, ready]);

  return { ports, loading, error };
}
