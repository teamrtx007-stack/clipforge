import { useState } from "react";
import type { Stats } from "../api";
import { plural } from "../format";

type Day = Stats["days"][number];

const label = (day: string) => new Date(`${day}T00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });

/** Clips made per day: one series, so no legend; hover or focus a day for its numbers. */
export function ActivityChart({ days }: { days: Day[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const most = Math.max(...days.map((d) => d.clips), 0);
  const step = [1, 2, 5, 10, 20, 50, 100, 200, 500].find((s) => most / s <= 4) ?? 1000;
  const top = Math.max(step * Math.ceil(most / step), step * 2);
  const ticks = Array.from({ length: top / step + 1 }, (_, i) => i * step);
  const shown = hover === null ? null : days[hover];

  return (
    <div className="chart">
      <div className="chart-plot" onMouseLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <div key={t} className="chart-tick" style={{ bottom: `${(t / top) * 100}%` }}><span>{t}</span></div>
        ))}
        <div className="chart-bars">
          {days.map((d, i) => (
            <button key={d.day} type="button" className="chart-col" aria-label={`${label(d.day)}: ${plural(d.clips, "clip")}`}
              onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} onBlur={() => setHover(null)}>
              {d.clips > 0 && <i style={{ height: `${(d.clips / top) * 100}%` }} />}
            </button>
          ))}
        </div>
        {shown && hover !== null && (
          <div className="chart-tip" style={{ left: `${Math.min(Math.max(((hover + 0.5) / days.length) * 100, 14), 86)}%` }}>
            {hover === days.length - 1 ? "Today" : label(shown.day)}: {plural(shown.clips, "clip")} made,{" "}
            {plural(shown.videos, "video")}, {plural(shown.moments, "live moment")}
          </div>
        )}
      </div>
      <div className="chart-x">
        {days.map((d, i) => (
          <span key={d.day}>{i === days.length - 1 ? "Today" : (days.length - 1 - i) % 2 === 0 ? label(d.day) : ""}</span>
        ))}
      </div>
      <table className="sr-only">
        <caption>Clips made per day</caption>
        <thead><tr><th>Day</th><th>Clips</th><th>Videos</th><th>Live moments</th></tr></thead>
        <tbody>
          {days.map((d) => <tr key={d.day}><td>{d.day}</td><td>{d.clips}</td><td>{d.videos}</td><td>{d.moments}</td></tr>)}
        </tbody>
      </table>
    </div>
  );
}
