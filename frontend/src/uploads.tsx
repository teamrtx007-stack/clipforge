import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import type { ClipLength, Source } from "./api";

// Uploads live above the router, so progress survives moving between pages.

export interface Upload {
  key: string;
  projectId: number;
  name: string;
  loaded: number;
  total: number;
  error?: string;
}

export interface UploadOptions {
  length?: ClipLength;
  template?: string;
}

interface Uploads {
  uploads: Upload[];
  start: (projectId: number, file: File, options?: UploadOptions) => Promise<Source>;
  dismiss: (key: string) => void;
}

const UploadsContext = createContext<Uploads | null>(null);

export function UploadsProvider({ children }: { children: ReactNode }) {
  const [uploads, setUploads] = useState<Upload[]>([]);

  const patch = useCallback((key: string, changes: Partial<Upload> | null) => {
    setUploads((all) => (changes ? all.map((u) => (u.key === key ? { ...u, ...changes } : u)) : all.filter((u) => u.key !== key)));
  }, []);

  const start = useCallback(
    (projectId: number, file: File, options: UploadOptions = {}) => {
      const key = `${projectId}-${file.name}-${Date.now()}`;
      setUploads((all) => [...all, { key, projectId, name: file.name, loaded: 0, total: file.size }]);
      const params = new URLSearchParams({ filename: file.name, ...options });
      return new Promise<Source>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", `/api/projects/${projectId}/upload?${params}`);
        xhr.upload.onprogress = (e) => patch(key, { loaded: e.loaded, total: e.total || file.size });
        xhr.onload = () => {
          if (xhr.status === 201) {
            patch(key, null);
            resolve(JSON.parse(xhr.responseText));
            return;
          }
          let error = `Upload failed (${xhr.status})`;
          try {
            error = JSON.parse(xhr.responseText).detail ?? error;
          } catch {
            // keep the status message
          }
          patch(key, { error });
          reject(new Error(error));
        };
        xhr.onerror = () => {
          patch(key, { error: "The connection dropped. Check that ClipForge is still running, then try again." });
          reject(new Error("network error"));
        };
        xhr.send(file);
      });
    },
    [patch],
  );

  const dismiss = useCallback((key: string) => patch(key, null), [patch]);

  return <UploadsContext.Provider value={{ uploads, start, dismiss }}>{children}</UploadsContext.Provider>;
}

export function useUploads(): Uploads {
  const ctx = useContext(UploadsContext);
  if (!ctx) throw new Error("useUploads must be used inside UploadsProvider");
  return ctx;
}
