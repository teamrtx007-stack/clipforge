// Key health and usage bookkeeping. Postgres (through Supabase's REST API with the service role) shares it
// across function instances; row level security with no policies keeps every other role out.
// The database only ever sees key ids, never keys.
import type { KeyState } from "./pool.ts";
import type { Fetch } from "./providers.ts";

export interface Usage {
  request_id: string;
  route: string;
  provider: string;
  key_id: string | null;
  status: number; // 200, the provider's error status, or 0 when it never answered
  latency_ms: number;
  input_tokens?: number | null;
  output_tokens?: number | null;
  audio_seconds?: number | null;
  error?: string | null;
}

export interface KeySummary {
  provider: string;
  key_id: string;
  ok_24h: number;
  failed_24h: number;
  tokens_24h: number;
  audio_seconds_24h: number;
}

export interface Store {
  states(): Promise<Map<string, KeyState>>;
  save(state: KeyState): Promise<void>;
  log(usage: Usage): Promise<void>;
  summary(): Promise<KeySummary[]>;
}

export class MemoryStore implements Store {
  private kept = new Map<string, KeyState>();
  readonly usage: (Usage & { at: number })[] = [];

  async states() {
    return new Map(this.kept);
  }
  async save(state: KeyState) {
    this.kept.set(state.key_id, state);
  }
  async log(usage: Usage) {
    this.usage.push({ ...usage, at: Date.now() });
    if (this.usage.length > 5000) this.usage.splice(0, 1000);
  }
  async summary() {
    const rows = new Map<string, KeySummary>();
    for (const u of this.usage.filter((u) => u.key_id && u.at > Date.now() - 86_400_000)) {
      const row = rows.get(u.key_id!) ??
        { provider: u.provider, key_id: u.key_id!, ok_24h: 0, failed_24h: 0, tokens_24h: 0, audio_seconds_24h: 0 };
      if (u.status === 200) row.ok_24h++;
      else row.failed_24h++;
      row.tokens_24h += (u.input_tokens ?? 0) + (u.output_tokens ?? 0);
      row.audio_seconds_24h += u.audio_seconds ?? 0;
      rows.set(u.key_id!, row);
    }
    return [...rows.values()];
  }
}

export class SupabaseStore implements Store {
  constructor(private url: string, private serviceKey: string, private fetch: Fetch) {}

  private async rest(path: string, init: RequestInit = {}): Promise<any> {
    const headers: Record<string, string> = {
      apikey: this.serviceKey,
      "content-type": "application/json",
      prefer: "return=minimal",
      ...(init.headers as Record<string, string> ?? {}),
    };
    if (!this.serviceKey.startsWith("sb_")) headers.authorization = `Bearer ${this.serviceKey}`; // legacy JWT keys
    const res = await this.fetch(`${this.url}/rest/v1/${path}`, {
      ...init,
      headers,
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) throw new Error(`database ${res.status}: ${(await res.text()).slice(0, 200)}`);
    return res.status === 204 || init.method ? null : res.json();
  }

  async states() {
    const rows: KeyState[] = await this.rest(
      "ai_key_state?select=key_id,provider,cooldown_until,reason,strikes,last_used_at",
    );
    return new Map(rows.map((r) => [r.key_id, r]));
  }
  async save(state: KeyState) {
    await this.rest("ai_key_state?on_conflict=key_id", {
      method: "POST",
      body: JSON.stringify([{ ...state, updated_at: new Date().toISOString() }]),
      headers: { prefer: "resolution=merge-duplicates,return=minimal" },
    });
  }
  async log(usage: Usage) {
    await this.rest("ai_usage", { method: "POST", body: JSON.stringify([usage]) });
    if (Math.random() < 0.005) { // now and then, forget usage older than 31 days
      await this.rest(`ai_usage?at=lt.${new Date(Date.now() - 31 * 86_400_000).toISOString()}`, { method: "DELETE" });
    }
  }
  async summary() {
    return await this.rest("ai_key_usage_24h?select=*") as KeySummary[];
  }
}

/** The database is bookkeeping. If it is down, requests still go through, tracked in this instance's memory. */
export class ResilientStore implements Store {
  private memory = new MemoryStore();
  private warned = false;

  constructor(private primary: Store | null) {}

  private async attempt<T>(work: (s: Store) => Promise<T>): Promise<T> {
    if (this.primary) {
      try {
        return await work(this.primary);
      } catch (err) {
        if (!this.warned) console.warn(JSON.stringify({ event: "store_degraded", error: String(err) }));
        this.warned = true;
      }
    }
    return work(this.memory);
  }

  async states() {
    const shared = await this.attempt((s) => s.states());
    const local = await this.memory.states();
    for (const [id, s] of local) { // this instance may know of a newer cooldown than the database
      if (!shared.has(id) || (s.last_used_at ?? "") > (shared.get(id)!.last_used_at ?? "")) shared.set(id, s);
    }
    return shared;
  }
  async save(state: KeyState) {
    await this.memory.save(state);
    if (this.primary) await this.attempt((s) => s.save(state));
  }
  async log(usage: Usage) {
    await this.attempt((s) => s.log(usage));
  }
  async summary() {
    return this.attempt((s) => s.summary());
  }
}
