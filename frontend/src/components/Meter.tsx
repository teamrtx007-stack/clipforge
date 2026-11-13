/** Combined excitement score for recent seconds. A moment fires when the line crosses the dashed threshold. */
export function Meter({ levels, threshold }: { levels: { t: number; score: number }[]; threshold: number }) {
  const top = Math.max(threshold * 1.5, ...levels.map((l) => l.score));
  const w = 90;
  const y = (v: number) => 56 - (v / top) * 52 - 2;
  const points = levels.map((l, i) => `${(i / Math.max(levels.length - 1, 1)) * w},${y(l.score)}`).join(" ");
  return (
    <svg className="meter" viewBox={`0 0 ${w} 56`} preserveAspectRatio="none" role="img"
      aria-label={levels.length ? `Excitement over the last ${levels.length} seconds` : "No signal yet"}>
      <line x1="0" x2={w} y1={y(threshold)} y2={y(threshold)} />
      {levels.length > 1 && <polyline points={points} />}
    </svg>
  );
}
