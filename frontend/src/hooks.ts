import { useCallback, useEffect, useRef, useState } from "react";
import { message } from "./api";

/** Load data now and again every `intervalMs`. Polling pauses while the tab is hidden. */
export function usePoll<T>(load: () => Promise<T>, intervalMs: number, key: unknown = null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (err) {
      setError(message(err));
    }
  }, []);

  useEffect(() => {
    setData(null);
    reload();
    const id = setInterval(() => {
      if (!document.hidden) reload();
    }, intervalMs);
    return () => clearInterval(id);
  }, [reload, intervalMs, key]);

  return { data, error, reload };
}
