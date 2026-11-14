import { Link } from "react-router-dom";
import { api } from "../api";
import { Icon } from "../components/Icon";
import { KeepNote } from "../components/KeepNote";
import { ago, plural } from "../format";
import { usePoll } from "../hooks";

export default function Videos() {
  const { data: projects, error } = usePoll(api.projects, 5000);
  const videos = projects?.filter((p) => !p.channel_id) ?? [];
  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="display">All videos</h1>
          <p className="subtitle">Videos you pasted or uploaded. Live clips are under each creator.</p>
        </div>
        <Link className="btn primary" to="/"><Icon name="plusCircle" size={15} />New video</Link>
      </div>
      <KeepNote />
      {error && <p className="error-text">Could not load videos: {error}</p>}
      {videos.length ? (
        <div className="projects">
          {videos.map((p) => (
            <Link key={p.id} to={`/projects/${p.id}`} className="project-card">
              <div className="frame">
                {p.cover_clip_id && <img src={`/media/clips/${p.cover_clip_id}/thumb.jpg`} alt="" loading="lazy" />}
              </div>
              <div>
                <div className="name">{p.name}</div>
                <div className="muted small">{plural(p.source_count, "video")} · {plural(p.clip_count, "clip")}</div>
                <div className="muted small">{ago(p.created_at)}</div>
              </div>
            </Link>
          ))}
        </div>
      ) : (
        projects && <div className="empty">No videos yet. <Link to="/">Paste a link or drop in a video</Link> to start.</div>
      )}
    </>
  );
}
