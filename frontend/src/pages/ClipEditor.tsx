import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, message, type Clip } from "../api";
import { ScoreMark } from "../components/ClipCard";
import { Bar } from "../components/Rows";
import { duration, timecode } from "../format";
import { usePoll } from "../hooks";

interface Form {
  title: string;
  description: string;
  hashtags: string;
  start: number;
  end: number;
  template: string;
}

const toForm = (c: Clip): Form => ({
  title: c.title,
  description: c.description,
  hashtags: c.hashtags.join(" "),
  start: c.start,
  end: c.end,
  template: c.template,
});

export default function ClipEditor() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const { data: clip, error, reload } = usePoll(() => api.clip(id), 2000, id);
  const { data: templates } = usePoll(api.templates, 600000);
  const [form, setForm] = useState<Form | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const sourcePlayer = useRef<HTMLVideoElement>(null);

  useEffect(() => setForm(null), [id]);
  useEffect(() => {
    if (clip && !form) setForm(toForm(clip));
  }, [clip, form]);

  if (error && !clip) return <p className="error-text">Could not load this clip: {error}</p>;
  if (!clip || !form) return <p className="muted">Loading…</p>;

  const total = clip.source.duration ?? form.end;
  const length = form.end - form.start;
  const lengthProblem = length < 3 || length > 180 ? "A clip must be 3 to 180 seconds long." : form.end > total + 0.05 ? "The end is past the end of the video." : null;
  const renderChanged = form.start !== clip.start || form.end !== clip.end || form.template !== clip.template;
  const textChanged = form.title !== clip.title || form.description !== clip.description || form.hashtags !== clip.hashtags.join(" ");
  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm({ ...form, [key]: value });

  async function save(render: boolean) {
    setBusy(true);
    setNote(null);
    try {
      const updated = await api.updateClip(id, {
        title: form!.title,
        description: form!.description,
        hashtags: form!.hashtags.split(/[\s,]+/).filter(Boolean),
        start: form!.start,
        end: form!.end,
        template: form!.template,
      });
      setForm(toForm(updated));
      if (render) await api.renderClip(id);
      await reload();
      setNote(render ? "Rendering started." : "Saved.");
    } catch (err) {
      setNote(message(err));
    } finally {
      setBusy(false);
    }
  }

  const playhead = () => Math.round((sourcePlayer.current?.currentTime ?? 0) * 10) / 10;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">
            Clip from <Link to={`/projects/${clip.source.project_id}`}>{clip.source.name}</Link> · {timecode(clip.start)} to {timecode(clip.end)}
          </div>
          <h2 className="display">{clip.title || "Untitled clip"}</h2>
        </div>
        <div className="card-actions">
          {clip.download_url && clip.status === "ready" && <a className="btn primary" href={clip.download_url} download>Download MP4</a>}
          <button className="btn danger" onClick={() => confirm("Delete this clip?") &&
            api.deleteClip(id).then(() => navigate(`/projects/${clip.source.project_id}`), (err) => alert(message(err)))}>
            Delete clip
          </button>
        </div>
      </div>

      <div className="editor">
        <div>
          <div className="frame">
            {clip.video_url ? (
              <video key={clip.video_url} src={clip.video_url} poster={clip.thumb_url ?? undefined} controls playsInline />
            ) : (
              <div className="state">{clip.status === "failed" ? `Render failed: ${clip.error}` : "Not rendered yet"}</div>
            )}
            <ScoreMark score={clip.score} />
          </div>
          {(clip.status === "rendering" || clip.status === "queued") && (
            <div className="panel" style={{ marginTop: 12 }}>
              <span className="mono muted">{clip.status === "queued" ? "Waiting to render" : `Rendering ${Math.round(clip.progress * 100)}%`}</span>
              <Bar value={clip.progress} />
            </div>
          )}
        </div>

        <div className="editor-side">
          <div className="panel">
            <h3>Why this clip</h3>
            {clip.reasons.length ? (
              <ul className="reasons">{clip.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
            ) : (
              <span className="muted">Picked for its overall mix of pace, topic and length.</span>
            )}
          </div>

          <div className="panel">
            <h3>Text for posting</h3>
            <label className="field">Title
              <input value={form.title} maxLength={200} onChange={(e) => set("title", e.target.value)} />
            </label>
            <label className="field">Description
              <textarea value={form.description} maxLength={2000} onChange={(e) => set("description", e.target.value)} />
            </label>
            <label className="field">Hashtags
              <input value={form.hashtags} onChange={(e) => set("hashtags", e.target.value)} placeholder="#shorts #podcast" />
            </label>
          </div>

          <div className="panel">
            <h3>Template</h3>
            <div className="templates" role="radiogroup" aria-label="Template">
              {templates?.map((t) => (
                <button key={t.id} type="button" role="radio" aria-checked={form.template === t.id}
                  className="template-option" onClick={() => set("template", t.id)}>
                  <b>{t.name}</b>
                  <span>{t.description}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="panel">
            <h3>Trim</h3>
            <div className="timeline" aria-hidden="true">
              <div className="range" style={{ left: `${(form.start / total) * 100}%`, width: `${(length / total) * 100}%` }} />
              <div className="ticks"><span>0:00</span><span>{timecode(total)}</span></div>
            </div>
            <div className="inline-fields" style={{ marginTop: 18 }}>
              <label className="field">Start (seconds)
                <input type="number" step={0.1} min={0} max={total} value={form.start}
                  onChange={(e) => set("start", e.target.valueAsNumber || 0)} />
              </label>
              <label className="field">End (seconds)
                <input type="number" step={0.1} min={0} max={total} value={form.end}
                  onChange={(e) => set("end", e.target.valueAsNumber || 0)} />
              </label>
              <span className="mono muted">{duration(Math.max(length, 0))} long</span>
            </div>
            {clip.source.video_url && (
              <>
                <video ref={sourcePlayer} className="source-player" src={clip.source.video_url} controls preload="metadata"
                  onLoadedMetadata={(e) => (e.currentTarget.currentTime = form.start)} />
                <div className="card-actions">
                  <button className="btn" type="button" onClick={() => set("start", playhead())}>Start here</button>
                  <button className="btn" type="button" onClick={() => set("end", playhead())}>End here</button>
                </div>
              </>
            )}
            {lengthProblem && <span className="error-text">{lengthProblem}</span>}
          </div>

          <div className="card-actions">
            <button className="btn primary" disabled={busy || !!lengthProblem} onClick={() => save(true)}>
              {renderChanged ? "Save and render" : "Render again"}
            </button>
            <button className="btn" disabled={busy || !!lengthProblem || !(textChanged || renderChanged)} onClick={() => save(false)}>
              Save changes
            </button>
            {note && <span className="muted" role="status">{note}</span>}
          </div>
        </div>
      </div>
    </>
  );
}
