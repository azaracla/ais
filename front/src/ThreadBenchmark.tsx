import { useState, useEffect, useCallback } from "react";
import {
  isMultiThreadSupported,
  getBenchmarkMode,
  getBenchmarkThreadCount,
  setBenchmarkThreadCount,
  initBenchmarkDuckDB,
  closeBenchmarkDuckDB,
  benchmarkQueryOnBenchmarkDB,
  createBenchmarkTableOnBenchmarkDB,
  dropBenchmarkTableOnBenchmarkDB,
  type BenchmarkResult,
} from "./duckdb";

interface BenchmarkState {
  singleThread: BenchmarkResult | null;
  multiThread: BenchmarkResult | null;
  isRunning: boolean;
  error: string | null;
  threadCount: number;
  dataReady: boolean;
}

const DEFAULT_BENCHMARK_QUERIES = [
  {
    name: "COUNT + GROUP BY",
    query: "SELECT category, COUNT(*) as cnt, AVG(value1) as avg_val FROM benchmark_data GROUP BY category ORDER BY cnt DESC",
  },
  {
    name: "SUM + AVG",
    query: "SELECT SUM(value1) as total, AVG(value2) as avg FROM benchmark_data",
  },
  {
    name: "Filter + Aggregate",
    query: "SELECT COUNT(*) as cnt FROM benchmark_data WHERE value1 > 50 AND value2 < 500",
  },
  {
    name: "Complex Join Self",
    query: "SELECT a.category, b.category as cat2, COUNT(*) as cnt FROM benchmark_data a, benchmark_data b WHERE a.id = b.id AND a.value1 > b.value1 GROUP BY a.category, b.category ORDER BY cnt DESC LIMIT 20",
  },
  {
    name: "Date Aggregation",
    query: "SELECT date_val, COUNT(*) as cnt, AVG(value1) as avg_val FROM benchmark_data GROUP BY date_val ORDER BY cnt DESC LIMIT 50",
  },
];

const ROW_COUNTS = [10000, 100000, 500000, 1000000];

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function formatNumber(n: number): string {
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return n.toString();
}

function ResultRow({
  label,
  singleThread,
  multiThread,
}: {
  label: string;
  singleThread: BenchmarkResult | null;
  multiThread: BenchmarkResult | null;
}) {
  const speedup = singleThread && multiThread
    ? singleThread.avg / multiThread.avg
    : null;

  return (
    <tr>
      <td style={{ textAlign: "left", padding: "8px 12px" }}>
        <strong>{label}</strong>
      </td>
      <td style={{ textAlign: "center", padding: "8px 12px" }}>
        {singleThread ? formatMs(singleThread.avg) : "-"}
      </td>
      <td style={{ textAlign: "center", padding: "8px 12px" }}>
        {multiThread ? formatMs(multiThread.avg) : "-"}
      </td>
      <td style={{ textAlign: "center", padding: "8px 12px" }}>
        {speedup !== null ? (
          <span style={{
            color: speedup > 1 ? "#22c55e" : speedup < 1 ? "#ef4444" : "#888",
            fontWeight: 600,
          }}>
            {speedup.toFixed(2)}x
          </span>
        ) : (
          "-"
        )}
      </td>
    </tr>
  );
}

export default function ThreadBenchmark() {
  const [state, setState] = useState<BenchmarkState>({
    singleThread: null,
    multiThread: null,
    isRunning: false,
    error: null,
    threadCount: 0,
    dataReady: false,
  });

  const [selectedQueryIndex, setSelectedQueryIndex] = useState(0);
  const [rowCount, setRowCount] = useState(100000);
  const [iterations, setIterations] = useState(3);
  const [selectedThreadCount, setSelectedThreadCount] = useState(4);
  const [customQuery, setCustomQuery] = useState("");
  const [useCustomQuery, setUseCustomQuery] = useState(false);

  // Vérifier le support du multithreading au montage
  useEffect(() => {
    const supported = isMultiThreadSupported();
    console.log("Multithreading supported:", supported, {
      crossOriginIsolated: self.crossOriginIsolated,
      hasSharedArrayBuffer: typeof SharedArrayBuffer !== "undefined",
    });
  }, []);

  // Charger le nombre de threads
  const loadThreadCount = useCallback(async () => {
    try {
      const count = await getBenchmarkThreadCount();
      setState((s) => ({ ...s, threadCount: count }));
    } catch (e) {
      console.warn("Could not load thread count:", e);
    }
  }, []);

  // Initialiser DuckDB de benchmark avec le mode souhaité
  const initBenchmarkWithMode = useCallback(
    async (mode: "threads" | "eh") => {
      setState((s) => ({ ...s, isRunning: true, error: null }));
      
      try {
        await initBenchmarkDuckDB(mode);
        await loadThreadCount();
        setState((s) => ({ ...s, isRunning: false }));
        return true;
      } catch (e: any) {
        setState((s) => ({
          ...s,
          isRunning: false,
          error: `Erreur d'initialisation (${mode}): ${e.message}`,
        }));
        return false;
      }
    },
    [loadThreadCount]
  );

  // Créer la table de benchmark
  const prepareBenchmarkData = useCallback(async () => {
    setState((s) => ({ ...s, isRunning: true, error: null }));
    
    try {
      // Initialiser la base de données de benchmark si ce n'est pas déjà fait
      await initBenchmarkDuckDB('threads');
      
      await dropBenchmarkTableOnBenchmarkDB();
      await createBenchmarkTableOnBenchmarkDB(rowCount);
      setState((s) => ({ ...s, dataReady: true, isRunning: false }));
    } catch (e: any) {
      setState((s) => ({
        ...s,
        isRunning: false,
        error: `Erreur création table: ${e.message}`,
      }));
    }
  }, [rowCount]);

  // Exécuter le benchmark
  const runBenchmark = useCallback(async () => {
    const queryObj = useCustomQuery
      ? { name: "Custom", query: customQuery }
      : DEFAULT_BENCHMARK_QUERIES[selectedQueryIndex];

    if (!queryObj.query.trim()) {
      setState((s) => ({ ...s, error: "La requête est vide" }));
      return;
    }

    setState((s) => ({
      ...s,
      isRunning: true,
      error: null,
      singleThread: null,
      multiThread: null,
    }));

    try {
      // Configuration du nombre de threads
      await setBenchmarkThreadCount(selectedThreadCount);

      // Benchmark en mode single-thread (EH)
      const ehInited = await initBenchmarkWithMode("eh");
      let singleThreadResult: BenchmarkResult | null = null;
      
      if (ehInited) {
        singleThreadResult = await benchmarkQueryOnBenchmarkDB(queryObj.query, iterations);
      }

      // Benchmark en mode multi-thread
      const threadsInited = await initBenchmarkWithMode("threads");
      let multiThreadResult: BenchmarkResult | null = null;
      
      if (threadsInited) {
        multiThreadResult = await benchmarkQueryOnBenchmarkDB(queryObj.query, iterations);
      }

      setState((s) => ({
        ...s,
        singleThread: singleThreadResult,
        multiThread: multiThreadResult,
        isRunning: false,
      }));
    } catch (e: any) {
      setState((s) => ({
        ...s,
        isRunning: false,
        error: `Erreur benchmark: ${e.message}`,
      }));
    }
  }, [
    selectedQueryIndex,
    customQuery,
    useCustomQuery,
    rowCount,
    iterations,
    selectedThreadCount,
    initBenchmarkWithMode,
  ]);

  // Charger le mode actuel au montage
  useEffect(() => {
    const checkMode = async () => {
      try {
        // Vérifier si la base de données de benchmark est déjà initialisée
        const mode = getBenchmarkMode();
        if (mode) {
          await loadThreadCount();
        }
      } catch (e) {
        console.warn("Could not check benchmark mode:", e);
      }
    };
    checkMode();
  }, [loadThreadCount]);

  // Nettoyer à la fin
  useEffect(() => {
    return () => {
      // Fermer la base de données de benchmark quand le composant est démonté
      closeBenchmarkDuckDB().catch(console.warn);
    };
  }, []);

  const currentMode = getBenchmarkMode();
  const multiThreadSupported = isMultiThreadSupported();

  return (
    <div
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: "rgba(255, 255, 255, 0.98)",
        backdropFilter: "blur(10px)",
        padding: 20,
        overflow: "auto",
        zIndex: 1000,
      }}
    >
      <div
        style={{
          maxWidth: 1200,
          margin: "0 auto",
        }}
      >
        <h1
          style={{
            fontSize: 28,
            fontWeight: 700,
            marginBottom: 20,
            color: "#111",
          }}
        >
          DuckDB-WASM Multi-threading Benchmark
        </h1>

        {/* Status Bar */}
        <div
          style={{
            display: "flex",
            gap: 16,
            marginBottom: 20,
            padding: "12px 16px",
            background: "#f8fafc",
            borderRadius: 8,
            border: "1px solid #e2e8f0",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <span
              style={{
                width: 12,
                height: 12,
                borderRadius: "50%",
                background: multiThreadSupported ? "#22c55e" : "#ef4444",
              }}
            />
            <span style={{ fontSize: 13, color: "#475569" }}>
              Multithreading: {multiThreadSupported ? "Supported" : "Not supported"}
            </span>
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <span
              style={{
                width: 12,
                height: 12,
                borderRadius: "50%",
                background: currentMode === "threads" ? "#22c55e" : "#fbbf24",
              }}
            />
            <span style={{ fontSize: 13, color: "#475569" }}>
              Mode: {currentMode === "threads" ? "Multi-thread" : currentMode === "eh" ? "Single-thread (EH)" : "Not initialized"}
            </span>
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <span style={{ fontSize: 13, color: "#475569" }}>
              Worker Threads: {state.threadCount}
            </span>
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <span
              style={{
                width: 12,
                height: 12,
                borderRadius: "50%",
                background: state.dataReady ? "#22c55e" : "#ef4444",
              }}
            />
            <span style={{ fontSize: 13, color: "#475569" }}>
              Benchmark Data: {state.dataReady ? "Ready" : "Not ready"}
            </span>
          </div>
        </div>

        {/* Configuration */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))",
            gap: 16,
            marginBottom: 20,
          }}
        >
          {/* Row Count */}
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
            }}
          >
            <label
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: "#111",
              }}
            >
              Rows in Table
            </label>
            <select
              value={rowCount}
              onChange={(e) => setRowCount(Number(e.target.value))}
              disabled={state.isRunning}
              style={{
                padding: "8px 12px",
                border: "1px solid #e2e8f0",
                borderRadius: 6,
                fontSize: 13,
                background: "#fff",
              }}
            >
              {ROW_COUNTS.map((count) => (
                <option key={count} value={count}>
                  {formatNumber(count)} rows
                </option>
              ))}
            </select>
          </div>

          {/* Thread Count */}
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
            }}
          >
            <label
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: "#111",
              }}
            >
              Thread Count (PRAGMA threads)
            </label>
            <input
              type="number"
              value={selectedThreadCount}
              onChange={(e) => setSelectedThreadCount(Math.max(1, Number(e.target.value) || 1))}
              min={1}
              max={16}
              disabled={state.isRunning}
              style={{
                padding: "8px 12px",
                border: "1px solid #e2e8f0",
                borderRadius: 6,
                fontSize: 13,
              }}
            />
          </div>

          {/* Iterations */}
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
            }}
          >
            <label
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: "#111",
              }}
            >
              Iterations
            </label>
            <select
              value={iterations}
              onChange={(e) => setIterations(Number(e.target.value))}
              disabled={state.isRunning}
              style={{
                padding: "8px 12px",
                border: "1px solid #e2e8f0",
                borderRadius: 6,
                fontSize: 13,
                background: "#fff",
              }}
            >
              {[1, 3, 5, 10].map((n) => (
                <option key={n} value={n}>
                  {n} iterations
                </option>
              ))}
            </select>
          </div>

          {/* Query Select */}
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
            }}
          >
            <label
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: "#111",
              }}
            >
              Query Type
            </label>
            <select
              value={useCustomQuery ? "custom" : selectedQueryIndex.toString()}
              onChange={(e) => {
                if (e.target.value === "custom") {
                  setUseCustomQuery(true);
                } else {
                  setUseCustomQuery(false);
                  setSelectedQueryIndex(Number(e.target.value));
                }
              }}
              disabled={state.isRunning}
              style={{
                padding: "8px 12px",
                border: "1px solid #e2e8f0",
                borderRadius: 6,
                fontSize: 13,
                background: "#fff",
              }}
            >
              {DEFAULT_BENCHMARK_QUERIES.map((q, idx) => (
                <option key={idx} value={idx}>
                  {q.name}
                </option>
              ))}
              <option value="custom">Custom Query</option>
            </select>
          </div>
        </div>

        {/* Custom Query Input */}
        {useCustomQuery && (
          <div
            style={{
              marginBottom: 20,
            }}
          >
            <textarea
              value={customQuery}
              onChange={(e) => setCustomQuery(e.target.value)}
              placeholder="Enter your SQL query..."
              disabled={state.isRunning}
              style={{
                width: "100%",
                padding: "12px 16px",
                border: "1px solid #e2e8f0",
                borderRadius: 8,
                fontSize: 13,
                fontFamily: "monospace",
                minHeight: 80,
                resize: "vertical",
                background: "#fff",
              }}
            />
          </div>
        )}

        {/* Actions */}
        <div
          style={{
            display: "flex",
            gap: 12,
            marginBottom: 20,
            flexWrap: "wrap",
          }}
        >
          <button
            onClick={prepareBenchmarkData}
            disabled={state.isRunning || state.dataReady}
            style={{
              padding: "10px 20px",
              border: "none",
              borderRadius: 8,
              background: state.dataReady ? "#94a3b8" : "#6366f1",
              color: "#fff",
              fontSize: 14,
              fontWeight: 600,
              cursor: state.dataReady || state.isRunning ? "not-allowed" : "pointer",
              transition: "background 0.2s",
            }}
          >
            {state.dataReady ? "Data Already Ready" : "Prepare Benchmark Data"}
          </button>

          <button
            onClick={runBenchmark}
            disabled={state.isRunning || !state.dataReady}
            style={{
              padding: "10px 20px",
              border: "none",
              borderRadius: 8,
              background: state.isRunning ? "#94a3b8" : "#22c55e",
              color: "#fff",
              fontSize: 14,
              fontWeight: 600,
              cursor: state.isRunning || !state.dataReady ? "not-allowed" : "pointer",
              transition: "background 0.2s",
            }}
          >
            {state.isRunning ? "Running..." : "Run Benchmark"}
          </button>

          <button
            onClick={async () => {
              await dropBenchmarkTableOnBenchmarkDB();
              setState((s) => ({ ...s, dataReady: false }));
            }}
            disabled={state.isRunning}
            style={{
              padding: "10px 20px",
              border: "none",
              borderRadius: 8,
              background: "#ef4444",
              color: "#fff",
              fontSize: 14,
              fontWeight: 600,
              cursor: state.isRunning ? "not-allowed" : "pointer",
              transition: "background 0.2s",
            }}
          >
            Clear Data
          </button>
        </div>

        {/* Error */}
        {state.error && (
          <div
            style={{
              padding: "12px 16px",
              background: "#fef2f2",
              border: "1px solid #fecaca",
              borderRadius: 8,
              color: "#dc2626",
              fontSize: 13,
              marginBottom: 20,
            }}
          >
            Error: {state.error}
          </div>
        )}

        {/* Results */}
        {state.singleThread || state.multiThread ? (
          <div
            style={{
              background: "#fff",
              border: "1px solid #e2e8f0",
              borderRadius: 12,
              padding: 20,
            }}
          >
            <h2
              style={{
                fontSize: 18,
                fontWeight: 700,
                marginBottom: 16,
                color: "#111",
              }}
            >
              Results
            </h2>
            <table
              style={{
                width: "100%",
                borderCollapse: "collapse",
              }}
            >
              <thead>
                <tr
                  style={{
                    background: "#f8fafc",
                    borderBottom: "2px solid #e2e8f0",
                  }}
                >
                  <th
                    style={{
                      textAlign: "left",
                      padding: "12px",
                      fontSize: 12,
                      fontWeight: 600,
                      color: "#64748b",
                      textTransform: "uppercase",
                      letterSpacing: "0.5px",
                    }}
                  >
                    Metric
                  </th>
                  <th
                    style={{
                      textAlign: "center",
                      padding: "12px",
                      fontSize: 12,
                      fontWeight: 600,
                      color: "#64748b",
                      textTransform: "uppercase",
                      letterSpacing: "0.5px",
                    }}
                  >
                    Single-thread (EH)
                  </th>
                  <th
                    style={{
                      textAlign: "center",
                      padding: "12px",
                      fontSize: 12,
                      fontWeight: 600,
                      color: "#64748b",
                      textTransform: "uppercase",
                      letterSpacing: "0.5px",
                    }}
                  >
                    Multi-thread
                  </th>
                  <th
                    style={{
                      textAlign: "center",
                      padding: "12px",
                      fontSize: 12,
                      fontWeight: 600,
                      color: "#64748b",
                      textTransform: "uppercase",
                      letterSpacing: "0.5px",
                    }}
                  >
                    Speedup
                  </th>
                </tr>
              </thead>
              <tbody>
                <ResultRow
                  label="Average"
                  singleThread={state.singleThread}
                  multiThread={state.multiThread}
                />
                <ResultRow
                  label="Minimum"
                  singleThread={state.singleThread}
                  multiThread={state.multiThread}
                />
                <ResultRow
                  label="Maximum"
                  singleThread={state.singleThread}
                  multiThread={state.multiThread}
                />
              </tbody>
            </table>

            <div
              style={{
                marginTop: 16,
                padding: "12px 16px",
                background: "#f8fafc",
                borderRadius: 8,
                fontSize: 12,
                color: "#64748b",
              }}
            >
              Query: {(useCustomQuery
                ? customQuery.slice(0, 100) + (customQuery.length > 100 ? "..." : "")
                : DEFAULT_BENCHMARK_QUERIES[selectedQueryIndex]?.query) || "-"}
              <br />
              Table size: {formatNumber(rowCount)} rows | Iterations: {iterations} | 
              Threads: {selectedThreadCount}
            </div>
          </div>
        ) : !state.isRunning && state.dataReady ? (
          <div
            style={{
              textAlign: "center",
              padding: 40,
              color: "#888",
              fontSize: 14,
            }}
          >
            Click &quot;Run Benchmark&quot; to compare single-thread vs multi-thread performance
          </div>
        ) : null}

        {/* Info */}
        <div
          style={{
            marginTop: 40,
            padding: "16px 20px",
            background: "#eff6ff",
            borderRadius: 8,
            border: "1px solid #bae6fd",
            fontSize: 13,
            color: "#0369a1",
          }}
        >
          <h3
            style={{
              fontSize: 14,
              fontWeight: 600,
              marginBottom: 8,
            }}
          >
            Notes:
          </h3>
          <ul
            style={{
              margin: 0,
              paddingLeft: 20,
              lineHeight: 1.6,
            }}
          >
            <li>
              <strong>COI Required:</strong> Cross-Origin Isolation headers must be configured 
              on your server for multi-threading to work.
            </li>
            <li>
              <strong>EH Mode:</strong> Embedded HTTP mode is single-threaded but works 
              without COI.
            </li>
            <li>
              <strong>Threads:</strong> The actual number of worker threads may be limited 
              by your CPU core count and browser settings.
            </li>
            <li>
              <strong>Performance:</strong> Multi-threading shows the most improvement on 
              complex queries with aggregations, joins, and filtering.
            </li>
          </ul>
        </div>
      </div>
    </div>
  );
}
