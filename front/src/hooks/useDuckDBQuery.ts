import { useState, useCallback, useRef, useEffect } from "react";

/**
 * Generic hook for executing DuckDB queries with loading and error states
 * 
 * @example
 * ```tsx
 * const { data, loading, error, execute } = useDuckDBQuery<Vessel[]>();
 * 
 * useEffect(() => {
 *   execute(() => queryLastPositions(date, bounds));
 * }, [date, bounds]);
 * ```
 */
export function useDuckDBQuery<T = unknown>(initialData?: T) {
  const [data, setData] = useState<T | undefined>(initialData);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const queryFnRef = useRef<(() => Promise<T>) | null>(null);
  const cancelRef = useRef<() => void>(() => {});

  // Cancel any pending query on unmount
  useEffect(() => {
    return () => {
      cancelRef.current();
    };
  }, []);

  const execute = useCallback(
    async (queryFn: () => Promise<T>, cancelToken?: { signal: AbortSignal }) => {
      // Store the query function for refresh
      queryFnRef.current = queryFn;
      
      // Cancel previous query if still running
      cancelRef.current();
      
      setLoading(true);
      setError(null);

      // Create a new abort controller for this query
      const abortController = new AbortController();
      cancelRef.current = () => abortController.abort();

      try {
        // Check if there's a signal from the caller
        if (cancelToken) {
          if (cancelToken.signal.aborted) {
            setLoading(false);
            return;
          }
          // Merge signals
          cancelToken.signal.addEventListener("abort", () => {
            abortController.abort();
          });
        }

        const result = await queryFn();
        
        // Only update state if query wasn't cancelled
        if (!abortController.signal.aborted) {
          setData(result);
        }
        
        return result;
      } catch (err: any) {
        // Don't set error if query was cancelled
        if (err.name !== "AbortError" && !abortController.signal.aborted) {
          setError(err.message || "Query failed");
          console.error("[useDuckDBQuery] Error:", err);
        }
        return undefined;
      } finally {
        if (!abortController.signal.aborted) {
          setLoading(false);
        }
      }
    },
    []
  );

  const refresh = useCallback(
    () => {
      if (queryFnRef.current) {
        return execute(queryFnRef.current);
      }
      return undefined;
    },
    [execute]
  );

  const reset = useCallback(() => {
    setData(initialData);
    setError(null);
    setLoading(false);
    cancelRef.current();
  }, [initialData]);

  return {
    data,
    loading,
    error,
    execute,
    refresh,
    reset,
  };
}

/**
 * Hook for paginated DuckDB queries
 */
export function usePaginatedDuckDBQuery<T = unknown>(initialData?: T[]) {
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [hasMore, setHasMore] = useState(true);
  
  const { data, loading, error, execute, reset } = useDuckDBQuery<T[]>(initialData);

  const loadMore = useCallback(
    async (queryFn: (offset: number, limit: number) => Promise<T[]>) => {
      const newOffset = page * pageSize;
      const result = await execute(() => queryFn(newOffset, pageSize));
      
      if (result && result.length > 0) {
        setPage((p) => p + 1);
        setHasMore(result.length === pageSize);
      } else {
        setHasMore(false);
      }
      
      return result;
    },
    [page, pageSize, execute]
  );

  const reload = useCallback(
    async (queryFn: (offset: number, limit: number) => Promise<T[]>) => {
      setPage(0);
      setHasMore(true);
      reset();
      return loadMore(queryFn);
    },
    [loadMore, reset]
  );

  return {
    data,
    loading,
    error,
    page,
    pageSize,
    hasMore,
    setPageSize,
    loadMore,
    reload,
    reset,
  };
}

/**
 * Hook for DuckDB queries with debouncing
 */
export function useDebouncedDuckDBQuery<T = unknown>(wait: number = 300) {
  const { data, loading, error, execute, reset } = useDuckDBQuery<T>();
  const timeoutRef = useRef<NodeJS.Timeout | null>(null);

  const debouncedExecute = useCallback(
    (queryFn: () => Promise<T>) => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
      
      return new Promise<T | undefined>((resolve) => {
        timeoutRef.current = setTimeout(async () => {
          const result = await execute(queryFn);
          resolve(result);
        }, wait);
      });
    },
    [execute, wait]
  );

  useEffect(() => {
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
    };
  }, []);

  return {
    data,
    loading,
    error,
    execute: debouncedExecute,
    reset,
  };
}
