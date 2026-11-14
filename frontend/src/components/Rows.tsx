import { api, message, type Source } from "../api";
import { bytes, duration } from "../format";
import type { Upload } from "../uploads";

export function Bar({ value }: { value: number }) {
  const pct = Math.round(Math.min(Math.max(value, 0), 1) * 100);
  return (
    <div className="bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${pct}%` }} />
    </div>
  );
}

export function UploadRow({ upload, onDismiss }: { upload: Upload; onDismiss: () => void }) {
  return (
    <div className="row">
      <div>
        <div className="name">{upload.name}</div>
        <div className={upload.error ? "detail error-text" : "detail"}>
          {upload.error ?? `Uploading ${bytes(upload.loaded)} of ${bytes(upload.total)}`}
        </div>
      </div>
      {upload.error ? <button className="btn quiet" onClick={onDismiss}>Dismiss</button> : <span className="tag">Upload</span>}
      {!upload.error && <Bar value={upload.total ? upload.loaded / upload.total : 0} />}
    </div>
  );
}

const STATUS = {
  uploading: "Uploading",
  queued: "Waiting in queue",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
} as const;

export function SourceRow({ source, clipCount, onChange }: { source: Source; clipCount: number; onChange: () => void }) {
  const act = (fn: () => Promise<unknown>) => () => fn().then(onChange, (err) => alert(message(err)));
  const busy = source.status === "processing" || source.status === "queued";
  const detail =
    source.status === "processing" ? source.stage
    : source.expired ? `${clipCount} clip${clipCount === 1 ? "" : "s"} · original video deleted after processing`
    : source.status === "ready" ? `${clipCount} clip${clipCount === 1 ? "" : "s"} found`
    : source.status === "failed" ? source.error
    : STATUS[source.status];
  return (
    <div className="row">
      <div>
        <div className="name">
          {source.name} {source.kind === "live" && <span className="tag live">Live moment</span>}
        </div>
        <div className={source.status === "failed" ? "detail error-text" : "detail"}>
          {source.duration ? `${duration(source.duration)} · ` : ""}
          {detail}
        </div>
      </div>
      <div className="card-actions">
        {source.status === "failed" && !source.expired && <button className="btn" onClick={act(() => api.reprocess(source.id))}>Try again</button>}
        {source.status === "ready" && !source.expired && (
          <button
            className="btn quiet"
            title="Find clips again with the project's current clip length, count and template"
            onClick={() =>
              confirm(`Find clips again with the current settings? This replaces the ${clipCount} clip${clipCount === 1 ? "" : "s"} of this video, including any edits.`) &&
              act(() => api.reprocess(source.id))()
            }
          >
            Find clips again
          </button>
        )}
        <button
          className="btn quiet danger"
          onClick={() => confirm(`Delete "${source.name}" and its clips?`) && act(() => api.deleteSource(source.id))()}
        >
          Delete
        </button>
      </div>
      {busy && <Bar value={source.progress} />}
    </div>
  );
}
