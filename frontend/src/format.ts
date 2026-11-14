export function timecode(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const hh = Math.floor(s / 3600);
  const mm = String(Math.floor(s / 60) % 60).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return hh ? `${hh}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function duration(seconds: number): string {
  return seconds < 60 ? `${Math.round(seconds)}s` : timecode(seconds);
}

export function bytes(n: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i > 1 ? 1 : 0)} ${units[i]}`;
}

export function ago(epochSeconds: number): string {
  const s = Date.now() / 1000 - epochSeconds;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString();
}

export const LENGTH_LABELS = {
  short: "Short, 15 to 30 s",
  medium: "Medium, 30 to 60 s",
  long: "Long, 60 to 90 s",
} as const;

export const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

export const CHANNEL_STATUS = {
  starting: "Checking",
  offline: "Waiting for live",
  live: "Live, not recording",
  recording: "Recording",
  error: "Error",
  disabled: "Paused",
  off: "Monitoring off",
} as const;
