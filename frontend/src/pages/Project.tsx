import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, message, type ProjectSettings } from "../api";
import { ClipCard } from "../components/ClipCard";
import { KeepNote } from "../components/KeepNote";
import { SourceRow, UploadRow } from "../components/Rows";
import { ClipBox } from "../components/ClipBox";
import { ClipOptions } from "../components/Selects";
import { usePoll } from "../hooks";
import { useUploads } from "../uploads";

export default function ProjectPage() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const uploads = useUploads();
  const { data: project, error, reload } = usePoll(() => api.project(id), 2500, id);
  const [renaming, setRenaming] = useState<string | null>(null);

  if (error && !project) return <p className="error-text">Could not load this project: {error}</p>;
  if (!project) return <p className="muted">Loading…</p>;

  const mine = uploads.uploads.filter((u) => u.projectId === id);
  const clipsBySource = (sid: number) => project.clips.filter((c) => c.source_id === sid).length;
  const save = (changes: { name?: string; settings?: ProjectSettings }) =>
    api.updateProject(id, changes).then(reload, (err) => alert(message(err)));

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">{project.channel ? `Live channel · ${project.channel.platform}` : "Project"}</div>
          {renaming === null ? (
            <h1 className="display" onDoubleClick={() => setRenaming(project.name)}>{project.name}</h1>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (renaming.trim()) save({ name: renaming.trim() });
                setRenaming(null);
              }}
            >
              <input autoFocus aria-label="Project name" value={renaming} maxLength={120}
                onChange={(e) => setRenaming(e.target.value)} onBlur={() => setRenaming(null)} />
            </form>
          )}
        </div>
        <div className="card-actions">
          <button className="btn quiet" onClick={() => setRenaming(project.name)}>Rename</button>
          {project.channel && <Link className="btn quiet" to="/live">Channel settings</Link>}
          <button
            className="btn danger"
            onClick={() =>
              confirm(`Delete "${project.name}" with all its videos and clips? This cannot be undone.`) &&
              api.deleteProject(id).then(() => navigate("/"), (err) => alert(message(err)))
            }
          >
            Delete project
          </button>
        </div>
      </div>

      {!project.channel && (
        <ClipBox
          onFiles={(files) => files.forEach((f) => uploads.start(id, f).then(reload, () => undefined))}
          onLink={(url) => api.addLink(id, url).then(reload)}>
          <ClipOptions value={project.settings} onChange={(settings) => save({ settings })} />
        </ClipBox>
      )}

      {(mine.length > 0 || project.sources.length > 0) && (
        <section>
          <div className="section-head">
            <h2 className="display">{project.channel ? "Moments" : "Videos"}</h2>
            {project.source_total > project.sources.length && (
              <span className="mono muted">Newest {project.sources.length} of {project.source_total}</span>
            )}
          </div>
          <div className="rows">
            {mine.map((u) => <UploadRow key={u.key} upload={u} onDismiss={() => uploads.dismiss(u.key)} />)}
            {project.sources.map((s) => (
              <SourceRow key={s.id} source={s} clipCount={clipsBySource(s.id)} onChange={reload} />
            ))}
          </div>
        </section>
      )}

      <section>
        <div className="section-head">
          <h2 className="display">Clips</h2>
          {project.clips.length > 0 && <span className="mono muted">Best first · circled = strong pick</span>}
        </div>
        <KeepNote />
        {project.clips.length ? (
          <div className="sheet">
            {project.clips.map((c) => <ClipCard key={c.id} clip={c} />)}
          </div>
        ) : (
          <div className="empty">
            {project.channel
              ? "Clips from this channel appear here while it streams."
              : project.sources.length
                ? "Clips appear here when a video finishes processing."
                : "Add a video or paste a link to find clips."}
          </div>
        )}
      </section>
    </>
  );
}
