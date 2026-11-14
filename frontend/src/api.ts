// Typed client for the ClipForge API. Shapes mirror backend/app/api.py.

export type ClipLength = "short" | "medium" | "long";
export type Sensitivity = "low" | "medium" | "high";

export interface ProjectSettings {
  length: ClipLength;
  max_clips: number;
  template: string;
}

export interface Project {
  id: number;
  name: string;
  settings: ProjectSettings;
  created_at: number;
  source_count: number;
  clip_count: number;
  cover_clip_id: number | null;
  channel_id: number | null;
}

export interface Source {
  id: number;
  project_id: number;
  kind: "upload" | "live";
  name: string;
  status: "uploading" | "queued" | "processing" | "ready" | "failed";
  stage: string;
  progress: number;
  duration: number | null;
  width: number | null;
  height: number | null;
  meta: { moment?: { score: number; reasons: string[] } };
  error: string | null;
  created_at: number;
  video_url: string | null;
  storage: StoredFile | null;
  expired: boolean;
}

/** Where a file is kept: primary storage (R2), or only the backup archive (B2). Null when only on this computer. */
export interface StoredFile {
  location: "primary" | "backup";
  provider: string;
  backup: "none" | "pending" | "verified" | "failed" | "skipped";
}

export interface Clip {
  id: number;
  source_id: number;
  start: number;
  end: number;
  score: number;
  title: string;
  description: string;
  hashtags: string[];
  reasons: string[];
  template: string;
  status: "draft" | "queued" | "rendering" | "ready" | "failed";
  progress: number;
  error: string | null;
  created_at: number;
  rendered_at: number | null;
  video_url: string | null;
  thumb_url: string | null;
  download_url: string | null;
  storage: StoredFile | null;
  project_id?: number;
  project_name?: string;
  source_kind?: Source["kind"];
}

export interface Channel {
  id: number;
  project_id: number;
  platform: "twitch" | "youtube" | "kick" | "direct";
  url: string;
  name: string;
  enabled: boolean;
  auto_record: boolean;
  auto_render: boolean;
  chat: boolean;
  speech: boolean;
  sensitivity: Sensitivity;
  template: string;
  buffer_minutes: number;
  threshold: number;
  runtime: {
    status: "starting" | "offline" | "live" | "recording" | "error" | "disabled" | "off";
    since: number;
    error: string | null;
    moments: number;
    last_moment_at: number | null;
    recorded_seconds: number;
    levels: { t: number; score: number }[];
  };
}

export interface ProjectDetail extends Omit<Project, "source_count" | "clip_count" | "cover_clip_id" | "channel_id"> {
  sources: Source[];
  source_total: number;
  clips: Clip[];
  channel: Omit<Channel, "runtime" | "threshold"> | null;
}

export interface Template {
  id: string;
  name: string;
  description: string;
  layout: "auto" | "fill" | "fit_blur";
  captions: boolean;
  punch_zoom: number;
}

export interface Job {
  id: number;
  kind: "analyze_source" | "render_clip";
  target_id: number;
  status: "queued" | "running" | "done" | "failed";
  attempts: number;
  error: string | null;
  updated_at: number;
  label: string | null;
  project_id: number | null;
  stage: string | null;
  progress: number | null;
}

export interface SystemInfo {
  whisper_model: string;
  llm: { enabled: boolean; model: string | null; host: string | null; fallbacks: string[] };
  transcription: string[];
  live_enabled: boolean;
  workers: number;
  keep_hours: number;
  disk_free_bytes: number;
  disk_total_bytes: number;
  storage: {
    primary: string;
    backup: string | null;
    providers: Record<string, { used_bytes: number; free_bytes: number; percent: number; warning: boolean; full: boolean; role: string }>;
    source_keep_hours: number;
    clip_keep_days: number;
    backup_keep_days: number;
  };
}

export interface Stats {
  days: { day: string; videos: number; clips: number; moments: number }[];
  clips_ready: number;
  jobs_active: number;
}

async function errorText(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map((d: { msg: string }) => d.msg).join("; ");
  } catch {
    // not JSON; fall through to the status line
  }
  return `${res.status} ${res.statusText}`;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errorText(res));
  return (res.status === 204 ? undefined : await res.json()) as T;
}

export const api = {
  projects: () => request<Project[]>("GET", "/api/projects"),
  project: (id: number) => request<ProjectDetail>("GET", `/api/projects/${id}`),
  createProject: (name: string, settings?: Partial<ProjectSettings>) =>
    request<Project>("POST", "/api/projects", { name, settings }),
  updateProject: (id: number, changes: { name?: string; settings?: ProjectSettings }) =>
    request<Project>("PATCH", `/api/projects/${id}`, changes),
  deleteProject: (id: number) => request<void>("DELETE", `/api/projects/${id}`),

  addLink: (projectId: number, url: string, settings?: Partial<ProjectSettings>) =>
    request<Source>("POST", `/api/projects/${projectId}/link`, { url, ...settings }),
  reprocess: (id: number) => request<Source>("POST", `/api/sources/${id}/reprocess`),
  deleteSource: (id: number) => request<void>("DELETE", `/api/sources/${id}`),

  recentClips: (limit = 12) => request<Clip[]>("GET", `/api/clips?limit=${limit}`),
  clip: (id: number) => request<Clip & { source: Source }>("GET", `/api/clips/${id}`),
  updateClip: (id: number, changes: Partial<Pick<Clip, "title" | "description" | "hashtags" | "start" | "end" | "template">>) =>
    request<Clip & { source: Source }>("PATCH", `/api/clips/${id}`, changes),
  renderClip: (id: number) => request<Clip>("POST", `/api/clips/${id}/render`),
  restoreClip: (id: number) => request<Clip>("POST", `/api/clips/${id}/restore`),
  deleteClip: (id: number) => request<void>("DELETE", `/api/clips/${id}`),

  templates: () => request<Template[]>("GET", "/api/templates"),
  jobs: () => request<Job[]>("GET", "/api/jobs"),
  system: () => request<SystemInfo>("GET", "/api/system"),
  stats: () => request<Stats>("GET", `/api/stats?days=14&tz_offset=${new Date().getTimezoneOffset()}`),

  channels: () => request<Channel[]>("GET", "/api/channels"),
  addChannel: (url: string, sensitivity: Sensitivity, template: string) =>
    request<Channel>("POST", "/api/channels", { url, sensitivity, template }),
  updateChannel: (id: number, changes: Partial<Omit<Channel, "id" | "project_id" | "platform" | "url" | "threshold" | "runtime">>) =>
    request<Channel>("PATCH", `/api/channels/${id}`, changes),
  deleteChannel: (id: number) => request<void>("DELETE", `/api/channels/${id}`),
};

export function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
