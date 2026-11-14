// Key pools: which keys exist, which one to use next, and what a failure means for the key that made it.
// Keys come only from Supabase secrets. Everything else (logs, database, responses) sees a key id:
// the first 12 hex characters of the key's SHA-256, which cannot be turned back into the key.

export const PROVIDERS = ["gemini", "groq", "zai", "cloudflare", "openrouter"] as const;
export type Provider = typeof PROVIDERS[number];

export interface Key {
  provider: Provider;
  id: string;
  secret: string;
  account?: string; // Cloudflare's account ID, part of its URL
}

export interface KeyState {
  key_id: string;
  provider: Provider;
  cooldown_until: string | null; // ISO time; the key is skipped until then
  reason: string | null; // why it is cooling down, shown by /health
  strikes: number; // failures in a row; each doubles the next cooldown
  last_used_at: string | null;
}

/** Secret name holding each provider's keys, comma or newline separated. */
export const SECRET_NAMES: Record<Provider, string> = {
  gemini: "GEMINI_API_KEYS",
  groq: "GROQ_API_KEYS",
  zai: "ZAI_API_KEYS",
  cloudflare: "CLOUDFLARE_API_TOKENS",
  openrouter: "OPENROUTER_API_KEYS",
};

export async function keyId(secret: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret));
  return [...new Uint8Array(digest)].slice(0, 6).map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function loadKeys(env: (name: string) => string | undefined): Promise<Map<Provider, Key[]>> {
  const pools = new Map<Provider, Key[]>();
  for (const provider of PROVIDERS) {
    const secrets = [...new Set((env(SECRET_NAMES[provider]) ?? "").split(/[\s,]+/).filter(Boolean))];
    const account = env("CLOUDFLARE_ACCOUNT_ID");
    if (!secrets.length || (provider === "cloudflare" && !account)) continue;
    pools.set(
      provider,
      await Promise.all(secrets.map(async (secret) => ({
        provider,
        secret,
        id: await keyId(secret),
        ...(provider === "cloudflare" ? { account } : {}),
      }))),
    );
  }
  return pools;
}

const time = (iso: string | null | undefined) => (iso ? Date.parse(iso) : 0);

/** Keys not cooling down, least recently used first, so traffic spreads evenly over every key. */
export function usable(keys: Key[], states: Map<string, KeyState>, now: number): Key[] {
  return keys
    .filter((k) => time(states.get(k.id)?.cooldown_until) <= now)
    .sort((a, b) => time(states.get(a.id)?.last_used_at) - time(states.get(b.id)?.last_used_at));
}

/** Soonest moment a cooling key becomes usable again, for Retry-After. */
export function nextReady(keys: Key[], states: Map<string, KeyState>, now: number): number | null {
  const times = keys.map((k) => time(states.get(k.id)?.cooldown_until)).filter((t) => t > now);
  return times.length ? Math.min(...times) : null;
}

const MINUTE = 60_000, HOUR = 60 * MINUTE;

/** What an upstream failure means for the key. Null means the request was at fault, not the key,
 * so the key keeps its place and the next provider gets the request instead. */
export function penalty(
  status: number,
  retryAfterMs: number | null,
  strikes: number,
): { cooldownMs: number; reason: string } | null {
  if (status === 401 || status === 403) return { cooldownMs: 24 * HOUR, reason: "key refused: rotate it" };
  if (status === 429) {
    const backoff = Math.min(MINUTE * 2 ** strikes, HOUR); // a spent daily quota keeps backing off
    return {
      cooldownMs: Math.min(Math.max(retryAfterMs ?? 0, backoff), 24 * HOUR),
      reason: "rate limit or free quota",
    };
  }
  if (status === 0 || status >= 500) {
    return { cooldownMs: Math.min(30_000 * 2 ** strikes, 10 * MINUTE), reason: "provider error" };
  }
  return null;
}

export function cooled(
  state: KeyState | undefined,
  key: Key,
  now: number,
  p: { cooldownMs: number; reason: string },
): KeyState {
  return {
    key_id: key.id,
    provider: key.provider,
    last_used_at: new Date(now).toISOString(),
    cooldown_until: new Date(now + p.cooldownMs).toISOString(),
    reason: p.reason,
    strikes: (state?.strikes ?? 0) + 1,
  };
}

export function healthy(key: Key, now: number): KeyState {
  return {
    key_id: key.id,
    provider: key.provider,
    last_used_at: new Date(now).toISOString(),
    cooldown_until: null,
    reason: null,
    strikes: 0,
  };
}
