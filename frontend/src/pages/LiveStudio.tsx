import { useState } from "react";
import { Link } from "react-router-dom";
import { api, message, type Channel, type Sensitivity } from "../api";
import { ClipCard } from "../components/ClipCard";
import { Meter } from "../components/Meter";
import { TemplateSelect } from "../components/Selects";
import { ago, CHANNEL_STATUS, timecode } from "../format";
import { usePoll } from "../hooks";

const SENSITIVITY: { value: Sensitivity; label: string }[] = [
  { value: "low", label: "Big moments only" },
  { value: "medium", label: "Balanced" },
  { value: "high", label: "Catch more" },
];

function ChannelCard({ channel, onChange }: { channel: Channel; onChange: () => void }) {
  const rt = channel.runtime;
  const update = (changes: Parameters<typeof api.updateChannel>[1]) =>
    api.updateChannel(channel.id, changes).then(onChange, (err) => alert(message(err)));
  const toggle = (key: "auto_record" | "auto_render" | "chat" | "speech", label: string, disabled = false) => (
    <label className="switch">
      <input type="checkbox" checked={channel[key] && !disabled} disabled={disabled}
        onChange={(e) => update({ [key]: e.target.checked })} />
      {label}
    </label>
  );
  return (
    <article className={rt.status === "recording" ? "channel on-air" : "channel"}>
      <header>
        <span className={`tally ${rt.status}`}>{CHANNEL_STATUS[rt.status]}</span>
        <label className="switch">
          <input type="checkbox" checked={channel.enabled} onChange={(e) => update({ enabled: e.target.checked })} />
          Watch
        </label>
      </header>
      <div>
        <div className="name">{channel.name}</div>
        <a className="mono muted" href={channel.url} target="_blank" rel="noreferrer">{channel.url}</a>
      </div>
      {rt.error && <div className="error-text" style={{ fontSize: 13 }}>{rt.error}</div>}
      <Meter levels={rt.levels} threshold={channel.threshold} />
      <div className="stats">
        <span>Recorded <b>{timecode(rt.recorded_seconds)}</b></span>
        <span>Moments <b>{rt.moments}</b></span>
        {rt.last_moment_at && <span>Last <b>{ago(rt.last_moment_at)}</b></span>}
      </div>
      <div className="toggles">
        {toggle("auto_record", "Record when live")}
        {toggle("auto_render", "Render clips")}
        {toggle("chat", channel.platform === "twitch" ? "Read chat" : "Chat (Twitch only)", channel.platform !== "twitch")}
        {toggle("speech", "Listen for hype")}
      </div>
      <div className="inline-fields">
        <label className="field">Sensitivity
          <select value={channel.sensitivity} onChange={(e) => update({ sensitivity: e.target.value as Sensitivity })}>
            {SENSITIVITY.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </label>
        <label className="field">Template
          <TemplateSelect value={channel.template} onChange={(template) => update({ template })} />
        </label>
      </div>
      <div className="card-actions">
        <Link className="btn" to={`/projects/${channel.project_id}`}>Open clips</Link>
        <button className="btn quiet danger"
          onClick={() => confirm(`Stop watching ${channel.name}? Its clips stay in the project.`) &&
            api.deleteChannel(channel.id).then(onChange, (err) => alert(message(err)))}>
          Remove
        </button>
      </div>
    </article>
  );
}

export default function LiveStudio() {
  const channels = usePoll(api.channels, 2000);
  const clips = usePoll(() => api.recentClips(36), 4000);
  const [url, setUrl] = useState("");
  const [sensitivity, setSensitivity] = useState<Sensitivity>("medium");
  const [template, setTemplate] = useState("viral");
  const [error, setError] = useState<string | null>(null);
  const liveClips = clips.data?.filter((c) => c.source_kind === "live") ?? [];
  const monitoringOff = channels.data?.some((c) => c.runtime.status === "off");

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.addChannel(url, sensitivity, template);
      setUrl("");
      channels.reload();
    } catch (err) {
      setError(message(err));
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Live AI clipping</div>
          <h1 className="display">Creators</h1>
        </div>
      </div>

      {monitoringOff && (
        <div className="banner alert">Live monitoring is turned off on this server. Start ClipForge without <code>LIVE_ENABLED=0</code> to watch channels.</div>
      )}

      <form className="add-channel" onSubmit={add}>
        <label className="field">Channel or stream link
          <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="twitch.tv/name, youtube.com/@name, kick.com/name or an .m3u8 URL" required />
        </label>
        <label className="field">Sensitivity
          <select value={sensitivity} onChange={(e) => setSensitivity(e.target.value as Sensitivity)}>
            {SENSITIVITY.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </label>
        <label className="field">Template
          <TemplateSelect value={template} onChange={setTemplate} />
        </label>
        <button className="btn primary" type="submit">Add creator</button>
      </form>
      {error && <p className="error-text">{error}</p>}
      <p className="muted" style={{ fontSize: 13 }}>
        ClipForge records while a channel is live, keeps the last few minutes in a rolling buffer, and cuts a clip when the
        audio, chat or speech spikes. Twitch chat is read anonymously. YouTube and Kick use audio and speech only.
      </p>

      <section>
        {channels.data?.length ? (
          <div className="channels">
            {channels.data.map((c) => <ChannelCard key={c.id} channel={c} onChange={channels.reload} />)}
          </div>
        ) : (
          channels.data && <div className="empty">Add a creator above. You can watch several at once.</div>
        )}
      </section>

      <section>
        <div className="section-head"><h2 className="display">Live moments</h2></div>
        {liveClips.length ? (
          <div className="sheet">{liveClips.map((c) => <ClipCard key={c.id} clip={c} />)}</div>
        ) : (
          <div className="empty">Moments appear here a minute or two after they happen on stream.</div>
        )}
      </section>
    </>
  );
}
