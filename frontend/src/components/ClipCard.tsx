import { useState } from "react";
import { Link } from "react-router-dom";
import { api, message, type Clip } from "../api";
import { duration, timecode } from "../format";

const PICK = 70; // editors circle their picks; so do we

export function ScoreMark({ score }: { score: number }) {
  const pick = score >= PICK;
  return (
    <span className={pick ? "score" : "score plain"} title={`Score ${score} of 100${pick ? ", a strong pick" : ""}`}>
      {pick && (
        <svg viewBox="0 0 52 52" aria-hidden="true">
          <path className="draw" d="M41 10C31 2 11 5 6 21 2 37 21 50 37 45 51 40 51 16 35 8c-4-2-9-2-13 0" />
        </svg>
      )}
      {score}
    </span>
  );
}

function FrameState({ clip }: { clip: Clip }) {
  const pct = Math.round(clip.progress * 100);
  switch (clip.status) {
    case "ready":
      return null;
    case "queued":
      return <div className="state">Waiting to render</div>;
    case "rendering":
      return (
        <div className="state">
          Rendering {pct}%
          <div className="bar"><i style={{ width: `${pct}%` }} /></div>
        </div>
      );
    case "draft":
      return <div className="state">{clip.rendered_at ? "Edited. Render again to update." : "Not rendered yet"}</div>;
    case "failed":
      return <div className="state"><b>Render failed</b><span>{clip.error?.split("\n")[0]}</span></div>;
  }
}

export function ClipCard({ clip, small = false }: { clip: Clip; small?: boolean }) {
  const [hover, setHover] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const preview = hover && !small && clip.status === "ready" && clip.video_url;
  const archived = clip.storage?.location === "backup";
  return (
    <article>
      <Link
        to={`/clips/${clip.id}`}
        className="frame"
        aria-label={`Open clip ${clip.title || clip.id}`}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
      >
        {preview ? (
          <video src={clip.video_url!} poster={clip.thumb_url ?? undefined} autoPlay muted loop playsInline />
        ) : (
          clip.thumb_url && <img src={clip.thumb_url} alt="" loading="lazy" />
        )}
        {archived ? <div className="state">{restoring ? "Restoring from backup…" : "In the backup archive"}</div> : <FrameState clip={clip} />}
        <ScoreMark score={clip.score} />
        <div className="edge">
          <span>{timecode(clip.start)}</span>
          <span>{duration(clip.end - clip.start)}</span>
        </div>
      </Link>
      {!small && (
        <div className="card-meta">
          <Link to={`/clips/${clip.id}`} className="title">{clip.title || "Untitled clip"}</Link>
          {clip.reasons[0] && <span className="why">{clip.reasons[0]}</span>}
          {clip.download_url && clip.status === "ready" && (
            <div className="card-actions">
              <a className="btn quiet" href={clip.download_url} download>Download MP4</a>
              {clip.storage?.backup === "verified" && <span className="tag">Backed up</span>}
            </div>
          )}
          {archived && (
            <div className="card-actions">
              <button className="btn quiet" disabled={restoring}
                onClick={() => {
                  setRestoring(true);
                  api.restoreClip(clip.id).catch((err) => {
                    setRestoring(false);
                    alert(message(err));
                  });
                }}>
                Restore
              </button>
            </div>
          )}
        </div>
      )}
    </article>
  );
}
