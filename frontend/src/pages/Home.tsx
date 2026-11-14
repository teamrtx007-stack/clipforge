import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, message, type ProjectSettings } from "../api";
import { ActivityChart } from "../components/ActivityChart";
import { ClipBox } from "../components/ClipBox";
import { Icon, type IconName } from "../components/Icon";
import { Bar } from "../components/Rows";
import { ClipOptions } from "../components/Selects";
import { bytes, CHANNEL_STATUS, plural, speechLabel, timecode } from "../format";
import { usePoll } from "../hooks";
import { useUploads } from "../uploads";

function Stat({ icon, value, label }: { icon: IconName; value: number; label: string }) {
  return (
    <div className="card stat">
      <span className="icon-tile"><Icon name={icon} /></span>
      <b>{value}</b>
      <span>{label}</span>
    </div>
  );
}

function greeting(): string {
  const hour = new Date().getHours();
  return hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
}

export default function Home() {
  const navigate = useNavigate();
  const uploads = useUploads();
  const { data: channels } = usePoll(api.channels, 5000);
  const { data: projects } = usePoll(api.projects, 8000);
  const { data: stats } = usePoll(api.stats, 8000);
  const { data: system } = usePoll(api.system, 60000);
  const [settings, setSettings] = useState<ProjectSettings>({ length: "medium", template: "viral", max_clips: 8 });
  const [creator, setCreator] = useState(0); // a creator's project id, or 0 to start a new project
  const [error, setError] = useState<string | null>(null);

  async function startFromLink(url: string) {
    const pid = creator || (await api.createProject(url.slice(0, 120), settings)).id; // renamed to the video's title
    try {
      await api.addLink(pid, url, settings);
    } catch (err) {
      if (!creator) await api.deleteProject(pid).catch(() => undefined);
      throw err;
    }
    navigate(`/projects/${pid}`);
  }

  async function startFromFiles(files: File[]) {
    setError(null);
    try {
      const name = files[0].name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim() || "New project";
      const pid = creator || (await api.createProject(name, settings)).id;
      // Upload errors show on the project page.
      files.forEach((f) => uploads.start(pid, f, { length: settings.length, template: settings.template }).catch(() => undefined));
      navigate(`/projects/${pid}`);
    } catch (err) {
      setError(message(err));
    }
  }

  const total = (key: "clips" | "videos" | "moments") => stats?.days.reduce((n, d) => n + d[key], 0) ?? 0;
  const clipsIn = new Map(projects?.map((p) => [p.id, p.clip_count]));
  const steps = [
    channels?.length === 0 && {
      title: "Add a creator", to: "/live",
      detail: "Watch a Twitch, YouTube or Kick channel. ClipForge clips its live moments by itself.",
    },
    projects?.length === 0 && total("clips") === 0 && {
      title: "Clip your first video", to: null,
      detail: "Paste a YouTube link in the box above, or drop in a video file.",
    },
    system && !system.llm.enabled && {
      title: "Turn on AI titles and scoring (optional)", to: "https://github.com/teamrtx007-stack/clipforge#ai-providers",
      detail: "Add a free Gemini, Groq, Cloudflare or OpenRouter key to .env for better titles, Hindi included.",
    },
  ].filter((s) => !!s);

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="display">Home</h1>
          <p className="subtitle">{greeting()}. {plural(channels?.length ?? 0, "creator")}.</p>
        </div>
      </div>

      <section className="hero">
        <h2>What do you want to clip today?</h2>
        <p>Paste a link or drop in videos. You get captioned shorts back.</p>
        <ClipBox onFiles={startFromFiles} onLink={startFromLink}>
          <label className="pill select-pill">
            <Icon name="user" size={14} />
            <select aria-label="Creator" value={creator} onChange={(e) => setCreator(Number(e.target.value))}>
              <option value={0}>No creator</option>
              {channels?.map((c) => <option key={c.id} value={c.project_id}>{c.name}</option>)}
            </select>
          </label>
          <ClipOptions value={settings} onChange={setSettings} />
        </ClipBox>
        {error && <p className="error-text">{error}</p>}
      </section>

      <div className="stats">
        <Stat icon="radio" value={channels?.filter((c) => c.runtime.status === "recording").length ?? 0} label="Recording now" />
        <Stat icon="scissors" value={stats?.days.at(-1)?.clips ?? 0} label="Clips today" />
        <Stat icon="clock" value={stats?.jobs_active ?? 0} label="In the queue" />
        <Stat icon="download" value={stats?.clips_ready ?? 0} label="Ready to download" />
      </div>

      <div className="split">
        <section className="card">
          <div className="card-head">
            <div>
              <h3>Activity</h3>
              <p>Last 14 days</p>
            </div>
            <dl className="totals">
              <div><dt>Clips made</dt><dd>{total("clips")}</dd></div>
              <div><dt>Videos</dt><dd>{total("videos")}</dd></div>
              <div><dt>Live moments</dt><dd>{total("moments")}</dd></div>
            </dl>
          </div>
          {stats && <ActivityChart days={stats.days} />}
        </section>

        <section className="card machine">
          <h3>Free disk space</h3>
          {system && (
            <>
              <b className="big">{bytes(system.disk_free_bytes)}</b>
              <div className="meter-label">
                <span>on this computer</span>
                <span>{Math.round((100 * system.disk_free_bytes) / system.disk_total_bytes)}% free</span>
              </div>
              <Bar value={system.disk_free_bytes / system.disk_total_bytes} />
              <dl className="facts">
                <div><dt>Cost</dt><dd>Free, ₹0</dd></div>
                <div><dt>Speech to text</dt><dd>{speechLabel(system)}</dd></div>
                <div><dt>AI titles and scoring</dt><dd>{system.llm.enabled ? [system.llm.host, ...system.llm.fallbacks].join(" → ") : "Off"}</dd></div>
                <div><dt>Videos kept for</dt><dd>{system.keep_hours ? plural(system.keep_hours, "hour") : "Always"}</dd></div>
              </dl>
              <div className="card-actions">
                <Link className="btn primary" to="/live">Add a creator</Link>
                <Link className="btn quiet" to="/queue">View queue <Icon name="arrow" size={14} /></Link>
              </div>
            </>
          )}
        </section>
      </div>

      {steps.length > 0 && (
        <section className="card">
          <h3>Next steps</h3>
          <p className="card-sub">Finish setting up ClipForge</p>
          <ol className="next-steps">
            {steps.map((s, i) => {
              const body = (
                <>
                  <span className="num">{i + 1}</span>
                  <span className="step-text"><b>{s.title}</b><span>{s.detail}</span></span>
                  {s.to && <Icon name="arrow" />}
                </>
              );
              return (
                <li key={s.title}>
                  {s.to?.startsWith("http") ? <a className="step" href={s.to} target="_blank" rel="noreferrer">{body}</a>
                    : s.to ? <Link className="step" to={s.to}>{body}</Link> : <div className="step">{body}</div>}
                </li>
              );
            })}
          </ol>
        </section>
      )}

      <section className="card">
        <h3>Creators</h3>
        <p className="card-sub">Each creator's status and results</p>
        {channels?.length ? (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr><th>Creator</th><th>Status</th><th>Auto-record</th><th className="num">Moments</th><th className="num">Clips</th><th className="num">Recorded</th></tr>
              </thead>
              <tbody>
                {channels.map((c) => (
                  <tr key={c.id}>
                    <td><Link to={`/projects/${c.project_id}`} className="creator"><span className="avatar">{c.name[0]?.toUpperCase()}</span>{c.name}</Link></td>
                    <td><span className={`status-pill ${c.runtime.status}`}>{CHANNEL_STATUS[c.runtime.status]}</span></td>
                    <td><span className={c.auto_record ? "status-pill on" : "status-pill"}>{c.auto_record ? "On" : "Off"}</span></td>
                    <td className="num">{c.runtime.moments}</td>
                    <td className="num">{clipsIn.get(c.project_id) ?? 0}</td>
                    <td className="num">{timecode(c.runtime.recorded_seconds)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          channels && (
            <div className="empty">
              No creators yet. <Link to="/live">Add a creator</Link> to clip their live streams automatically.
            </div>
          )
        )}
      </section>
    </>
  );
}
