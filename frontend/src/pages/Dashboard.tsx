import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, message, type ClipLength } from "../api";
import { ClipCard } from "../components/ClipCard";
import { LengthSelect, TemplateSelect } from "../components/Selects";
import { UploadStrip } from "../components/UploadStrip";
import { ago, bytes } from "../format";
import { usePoll } from "../hooks";
import { useUploads } from "../uploads";

export default function Dashboard() {
  const navigate = useNavigate();
  const uploads = useUploads();
  const projects = usePoll(api.projects, 5000);
  const clips = usePoll(() => api.recentClips(12), 5000);
  const { data: system } = usePoll(api.system, 60000);
  const [length, setLength] = useState<ClipLength>("medium");
  const [template, setTemplate] = useState("viral");
  const [error, setError] = useState<string | null>(null);

  async function startProject(files: File[]) {
    setError(null);
    try {
      const name = files[0].name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim() || "New project";
      const project = await api.createProject(name, { length, template });
      files.forEach((f) => uploads.start(project.id, f).catch(() => undefined)); // errors show on the project page
      navigate(`/projects/${project.id}`);
    } catch (err) {
      setError(message(err));
    }
  }

  return (
    <>
      <UploadStrip
        title="Drop a long video. Get shorts."
        hint="ClipForge finds the moments worth sharing and cuts them into vertical clips with captions, face tracking and titles. Everything runs on this computer, for free."
        onFiles={startProject}
      >
        <LengthSelect value={length} onChange={setLength} />
        <TemplateSelect value={template} onChange={setTemplate} />
      </UploadStrip>
      {error && <p className="error-text">{error}</p>}

      <section>
        <div className="section-head">
          <h2 className="display">Latest clips</h2>
        </div>
        {clips.data?.length ? (
          <div className="sheet small">
            {clips.data.map((c) => <ClipCard key={c.id} clip={c} small />)}
          </div>
        ) : (
          <div className="empty">Clips appear here as soon as they are ready.</div>
        )}
      </section>

      <section>
        <div className="section-head">
          <h2 className="display">Projects</h2>
          {system && <span className="mono muted">{bytes(system.disk_free_bytes)} free on disk</span>}
        </div>
        {projects.error && <p className="error-text">Could not load projects: {projects.error}</p>}
        {projects.data?.length ? (
          <div className="projects">
            {projects.data.map((p) => (
              <Link key={p.id} to={`/projects/${p.id}`} className="project-card">
                <div className="frame">
                  {p.cover_clip_id && <img src={`/media/clips/${p.cover_clip_id}/thumb.jpg`} alt="" loading="lazy" />}
                </div>
                <div>
                  <div className="name">{p.name}</div>
                  <div className="mono muted">
                    {p.channel_id ? "Live channel · " : `${p.source_count} video${p.source_count === 1 ? "" : "s"} · `}
                    {p.clip_count} clip{p.clip_count === 1 ? "" : "s"}
                  </div>
                  <div className="mono muted">{ago(p.created_at)}</div>
                </div>
              </Link>
            ))}
          </div>
        ) : (
          projects.data && <div className="empty">No projects yet. Drop a video above to start one.</div>
        )}
      </section>
    </>
  );
}
