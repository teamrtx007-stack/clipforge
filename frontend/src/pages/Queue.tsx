import { Link } from "react-router-dom";
import { api } from "../api";
import { Bar } from "../components/Rows";
import { ago } from "../format";
import { usePoll } from "../hooks";

const KIND = { analyze_source: "Find clips", render_clip: "Render" } as const;

export default function Queue() {
  const { data: jobs, error } = usePoll(api.jobs, 2000);
  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Processing</div>
          <h1 className="display">Queue</h1>
        </div>
        <p className="muted" style={{ maxWidth: "48ch", margin: 0 }}>
          Jobs run one at a time on this computer. Live moments go first so clips appear while the stream is on.
        </p>
      </div>
      {error && <p className="error-text">{error}</p>}
      {jobs?.length ? (
        <table className="jobs">
          <thead>
            <tr><th>Job</th><th>Item</th><th>Status</th><th>Updated</th></tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}>
                <td>{KIND[j.kind]}</td>
                <td>
                  {j.project_id ? <Link to={j.kind === "render_clip" ? `/clips/${j.target_id}` : `/projects/${j.project_id}`}>{j.label || "Untitled"}</Link>
                    : <span className="muted">Deleted</span>}
                </td>
                <td style={{ minWidth: 160 }}>
                  {j.status === "running" ? (
                    <>
                      <div>{j.stage || "Working"} · {Math.round((j.progress ?? 0) * 100)}%</div>
                      <Bar value={j.progress ?? 0} />
                    </>
                  ) : j.status === "failed" ? (
                    <span className="error-text" title={j.error ?? ""}>Failed: {j.error?.split("\n")[0]}</span>
                  ) : (
                    <span className="muted">{j.status === "queued" ? "Waiting" : "Done"}</span>
                  )}
                </td>
                <td className="mono muted">{ago(j.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        jobs && <div className="empty">Nothing in the queue.</div>
      )}
    </>
  );
}
