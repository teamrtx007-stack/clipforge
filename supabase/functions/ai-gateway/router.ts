// The gateway's routes. Server to server only: one bearer token (GATEWAY_TOKEN) from the ClipForge worker,
// browsers refused outright, keys never leave this function, every upstream message redacted.
//
//   GET  /v1/health                                   keys per provider: ready, cooling or refused; 24 h usage
//   POST /v1/health/probe                             one tiny request per key, to check them after setup or rotation
//   POST /v1/chat/completions                         text, routed over every provider with keys
//   POST /v1/providers/{provider}/chat/completions    text, one provider only
//   POST /v1/audio/transcriptions                     speech to text, routed over Groq then Cloudflare
//   POST /v1/providers/{provider}/audio/transcriptions
import {
  cooled,
  healthy,
  type Key,
  type KeyState,
  nextReady,
  penalty,
  type Provider,
  PROVIDERS,
  usable,
} from "./pool.ts";
import {
  type AudioInput,
  chat,
  type ChatInput,
  type Fetch,
  type Message,
  modelFor,
  SPEECH_PROVIDERS,
  TEXT_PROVIDERS,
  transcribe,
  UpstreamError,
} from "./providers.ts";
import type { Store } from "./store.ts";

export interface Deps {
  pools: Map<Provider, Key[]>;
  store: Store;
  fetch: Fetch;
  env: (name: string) => string | undefined;
  token: string | undefined;
  now?: () => number;
}

type Kind = "chat" | "audio" | "probe";
const LIMITS = {
  chatBytes: 512 * 1024,
  audioBytes: 25 * 1024 * 1024,
  messages: 50,
  contentChars: 200_000,
  maxTokens: 8192,
};
const DEADLINE: Record<Kind, number> = { chat: 110_000, audio: 140_000, probe: 30_000 }; // Supabase stops a free-plan function at 150 s
const PER_ATTEMPT: Record<Kind, number> = { chat: 45_000, audio: 120_000, probe: 25_000 };

class GatewayError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly extra: Record<string, unknown> = {},
    readonly headers: Record<string, string> = {},
  ) {
    super(message);
  }
}

function reply(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      ...headers,
    },
  });
}

/** Upstream errors can echo the key they refused. Strip every configured secret before anything is logged or returned. */
function redact(text: string, deps: Deps): string {
  let out = text;
  for (const keys of deps.pools.values()) for (const k of keys) out = out.split(k.secret).join("[redacted]");
  if (deps.token) out = out.split(deps.token).join("[redacted]");
  return out.replace(/\s+/g, " ").slice(0, 300);
}

async function sha256(text: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

/** Constant-time check of the bearer token: compares digests, so neither length nor content leaks through timing. */
async function authorized(header: string | null, token: string): Promise<boolean> {
  const given = header?.startsWith("Bearer ") ? header.slice(7) : "";
  const [a, b] = await Promise.all([sha256(given), sha256(token)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0 && given.length > 0;
}

async function readChat(req: Request, pinned: boolean): Promise<ChatInput> {
  if (Number(req.headers.get("content-length") ?? 0) > LIMITS.chatBytes) {
    throw new GatewayError(413, "too_large", "Request body is over 512 KB");
  }
  const text = await req.text();
  if (text.length > LIMITS.chatBytes) throw new GatewayError(413, "too_large", "Request body is over 512 KB");
  let body: any;
  try {
    body = JSON.parse(text);
  } catch {
    throw new GatewayError(400, "bad_json", "Body must be JSON");
  }
  const messages: Message[] = body?.messages;
  const valid = Array.isArray(messages) && messages.length >= 1 && messages.length <= LIMITS.messages &&
    messages.every((m) => ["system", "user", "assistant"].includes(m?.role) && typeof m.content === "string") &&
    messages.reduce((n, m) => n + m.content.length, 0) <= LIMITS.contentChars;
  if (!valid) {
    throw new GatewayError(
      400,
      "bad_messages",
      `messages: 1 to ${LIMITS.messages} items of {role: system|user|assistant, content: string}, ${LIMITS.contentChars} characters in all`,
    );
  }
  const maxTokens = body.max_tokens ?? 2048, temperature = body.temperature ?? 0.4;
  if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > LIMITS.maxTokens) {
    throw new GatewayError(400, "bad_max_tokens", `max_tokens: an integer from 1 to ${LIMITS.maxTokens}`);
  }
  if (typeof temperature !== "number" || temperature < 0 || temperature > 2) {
    throw new GatewayError(400, "bad_temperature", "temperature: 0 to 2");
  }
  return {
    messages: messages.map(({ role, content }) => ({ role, content })),
    max_tokens: maxTokens,
    temperature,
    json: body.response_format?.type === "json_object",
    // A model name only means something once the provider is fixed; on the routed endpoint each provider uses its own.
    model: pinned && typeof body.model === "string" && body.model !== "auto" ? body.model.slice(0, 100) : undefined,
  };
}

async function readAudio(req: Request): Promise<AudioInput> {
  if (Number(req.headers.get("content-length") ?? 0) > LIMITS.audioBytes + 64 * 1024) {
    throw new GatewayError(413, "too_large", "Audio is over 25 MB; send shorter chunks");
  }
  let form: FormData;
  try {
    form = await req.formData();
  } catch {
    throw new GatewayError(400, "bad_form", "Send multipart/form-data with a file field");
  }
  const file = form.get("file"), language = form.get("language");
  if (!(file instanceof Blob) || file.size === 0) {
    throw new GatewayError(400, "no_file", "The file field is missing or empty");
  }
  if (file.size > LIMITS.audioBytes) {
    throw new GatewayError(413, "too_large", "Audio is over 25 MB; send shorter chunks");
  }
  if (language !== null && !/^[a-z]{2,3}$/.test(String(language))) {
    throw new GatewayError(400, "bad_language", "language: an ISO 639-1 code such as hi");
  }
  return {
    file,
    filename: file instanceof File && file.name ? file.name : "audio.mp3",
    ...(language ? { language: String(language) } : {}),
  };
}

/** Providers to try, in order. A pinned provider must have keys; otherwise the configured order, filtered to those with keys. */
function order(kind: Kind, pinned: string | null, deps: Deps): Provider[] {
  const able = kind === "chat" ? TEXT_PROVIDERS : SPEECH_PROVIDERS;
  if (pinned !== null) {
    if (!able.includes(pinned as Provider)) {
      throw new GatewayError(
        404,
        "unknown_provider",
        `${pinned} cannot do ${kind === "chat" ? "text" : "speech to text"}. Use one of: ${able.join(", ")}`,
      );
    }
    if (!deps.pools.get(pinned as Provider)?.length) {
      throw new GatewayError(503, "no_keys", `No keys are set for ${pinned}`);
    }
    return [pinned as Provider];
  }
  const configured = deps.env(kind === "chat" ? "AI_TEXT_ORDER" : "AI_SPEECH_ORDER")?.split(",").map((s) => s.trim())
    .filter(Boolean);
  const chosen = (configured?.length ? configured : able).filter((p) =>
    able.includes(p as Provider) && deps.pools.get(p as Provider)?.length
  );
  if (!chosen.length) {
    throw new GatewayError(
      503,
      "no_keys",
      `No provider keys are set for ${kind === "chat" ? "text" : "speech to text"}`,
    );
  }
  return chosen as Provider[];
}

interface Attempt {
  provider: string;
  key_id: string;
  status: number;
  error: string;
}

/** Try each provider's usable keys, least recently used first, until one answers or the deadline nears.
 * A failure caused by the key (refused, rate limited, provider down) cools that key and moves to the next key.
 * A failure caused by the request (400, 404, 413, 422) moves straight to the next provider. */
async function run<T>(
  kind: Kind,
  route: string,
  providers: Provider[],
  requestId: string,
  deps: Deps,
  work: (key: Key, signal: AbortSignal) => Promise<T>,
  measure: (
    result: T,
  ) => Partial<{ input_tokens: number | null; output_tokens: number | null; audio_seconds: number | null }>,
): Promise<{ result: T; key: Key }> {
  const now = deps.now ?? Date.now;
  const deadline = now() + DEADLINE[kind];
  const states: Map<string, KeyState> = await deps.store.states();
  const attempts: Attempt[] = [];
  outer:
  for (const provider of providers) {
    for (const key of usable(deps.pools.get(provider) ?? [], states, now())) {
      const remaining = deadline - now();
      if (remaining < 3000) break outer;
      const started = now();
      try {
        const result = await work(key, AbortSignal.timeout(Math.min(PER_ATTEMPT[kind], remaining)));
        const state = healthy(key, now());
        states.set(key.id, state);
        await deps.store.save(state);
        await deps.store.log({
          request_id: requestId,
          route,
          provider,
          key_id: key.id,
          status: 200,
          latency_ms: now() - started,
          ...measure(result),
        });
        return { result, key };
      } catch (err) {
        if (!(err instanceof UpstreamError)) throw err; // our bug, not the provider's: fail loudly
        const previous = states.get(key.id);
        const p = penalty(err.status, err.retryAfterMs, previous?.strikes ?? 0);
        if (p) {
          const state = cooled(previous, key, now(), p);
          states.set(key.id, state);
          await deps.store.save(state);
        }
        const error = redact(err.message, deps);
        attempts.push({ provider, key_id: key.id, status: err.status, error });
        await deps.store.log({
          request_id: requestId,
          route,
          provider,
          key_id: key.id,
          status: err.status,
          latency_ms: now() - started,
          error,
        });
        console.warn(
          JSON.stringify({
            event: "upstream_failed",
            request_id: requestId,
            provider,
            key_id: key.id,
            status: err.status,
          }),
        );
        if (!p) break; // every key of this provider would fail the same request
        // Overloaded, down or timed out: the provider's other keys would wait or fail the same way.
        if (err.status === 0 || err.status >= 500) break;
      }
    }
  }
  const ready = providers.map((p) => nextReady(deps.pools.get(p) ?? [], states, now())).filter((t): t is number =>
    t !== null
  );
  const retry: Record<string, string> = ready.length
    ? { "retry-after": String(Math.max(1, Math.ceil((Math.min(...ready) - now()) / 1000))) }
    : {};
  if (!attempts.length) {
    throw new GatewayError(
      503,
      "all_keys_cooling",
      "Every key is cooling down after rate limits or errors; try again later",
      {},
      retry,
    );
  }
  throw new GatewayError(502, "all_providers_failed", "No provider could answer", { attempts }, retry);
}

async function health(deps: Deps): Promise<Response> {
  const now = (deps.now ?? Date.now)();
  const [states, usage] = await Promise.all([deps.store.states(), deps.store.summary()]);
  const providers = Object.fromEntries(PROVIDERS.map((provider) => [provider, {
    text_model: TEXT_PROVIDERS.includes(provider) ? modelFor("text", provider, deps.env) : null,
    speech_model: SPEECH_PROVIDERS.includes(provider) ? modelFor("speech", provider, deps.env) : null,
    keys: (deps.pools.get(provider) ?? []).map((k) => {
      const s = states.get(k.id), u = usage.find((r) => r.key_id === k.id);
      const cooling = s?.cooldown_until && Date.parse(s.cooldown_until) > now;
      return {
        key_id: k.id,
        state: !cooling ? "ready" : s!.reason?.startsWith("key refused") ? "refused" : "cooling",
        cooldown_until: cooling ? s!.cooldown_until : null,
        reason: cooling ? s!.reason : null,
        ok_24h: u?.ok_24h ?? 0,
        failed_24h: u?.failed_24h ?? 0,
        tokens_24h: u?.tokens_24h ?? 0,
        audio_seconds_24h: u?.audio_seconds_24h ?? 0,
      };
    }),
  }]));
  return reply(200, {
    ok: true,
    providers,
    text_order: safeOrder("chat", deps),
    speech_order: safeOrder("audio", deps),
  });
}

function safeOrder(kind: Kind, deps: Deps): Provider[] {
  try {
    return order(kind, null, deps);
  } catch {
    return [];
  }
}

async function probe(deps: Deps, requestId: string): Promise<Response> {
  const input: ChatInput = {
    messages: [{ role: "user", content: 'Reply with the word "ok".' }],
    max_tokens: 600,
    temperature: 0,
    json: false,
  };
  const keys = TEXT_PROVIDERS.flatMap((provider) => (deps.pools.get(provider) ?? []).map((key) => ({ provider, key })));
  // All keys at once, so the check takes as long as the slowest key rather than all of them added up.
  const results = await Promise.all(keys.map(async ({ provider, key }) => {
    const started = (deps.now ?? Date.now)();
    try {
      await run(
        "probe",
        "probe",
        [provider],
        requestId,
        { ...deps, pools: new Map([[provider, [key]]]), store: freshStates(deps.store) }, // this key only, cooldown ignored
        (k, signal) => chat(k, input, deps.env, deps.fetch, signal),
        () => ({}),
      );
      return { provider, key_id: key.id, ok: true, latency_ms: (deps.now ?? Date.now)() - started };
    } catch (err) {
      const attempt = (err as GatewayError).extra?.attempts as Attempt[] | undefined;
      return {
        provider,
        key_id: key.id,
        ok: false,
        status: attempt?.[0]?.status ?? null,
        error: attempt?.[0]?.error ?? (err as Error).message,
      };
    }
  }));
  return reply(200, { results });
}

/** The probe tests every key even if it is cooling down, and still records what it learns. */
function freshStates(store: Store): Store {
  return {
    states: async () => new Map(),
    save: (s) => store.save(s),
    log: (u) => store.log(u),
    summary: () => store.summary(),
  };
}

export async function handle(req: Request, deps: Deps): Promise<Response> {
  const requestId = crypto.randomUUID();
  const ids = { "x-request-id": requestId };
  try {
    if (req.headers.has("origin")) {
      throw new GatewayError(403, "browser_refused", "The AI gateway is server to server only");
    }
    if (!deps.token) {
      throw new GatewayError(503, "not_configured", "GATEWAY_TOKEN is not set; the gateway refuses all requests");
    }
    if (!(await authorized(req.headers.get("authorization"), deps.token))) {
      throw new GatewayError(401, "unauthorized", "Missing or wrong bearer token", {}, {
        "www-authenticate": "Bearer",
      });
    }
    const path = new URL(req.url).pathname.replace(/^.*?\/ai-gateway(?=\/|$)/, "").replace(/\/+$/, "") || "/";
    const pinned = path.match(/^\/v1\/providers\/([a-z]+)\/(chat\/completions|audio\/transcriptions)$/);
    const route = pinned ? `/v1/${pinned[2]}` : path;
    const allowed: Record<string, string> = {
      "/v1/health": "GET",
      "/v1/health/probe": "POST",
      "/v1/chat/completions": "POST",
      "/v1/audio/transcriptions": "POST",
    };
    if (!allowed[route]) throw new GatewayError(404, "not_found", `No route ${path}`);
    if (req.method !== allowed[route]) {
      throw new GatewayError(405, "method_not_allowed", `Use ${allowed[route]}`, {}, { allow: allowed[route] });
    }

    if (route === "/v1/health") return await health(deps);
    if (route === "/v1/health/probe") return await probe(deps, requestId);

    if (route === "/v1/chat/completions") {
      const providers = order("chat", pinned?.[1] ?? null, deps); // an unknown provider is a 404 before the body is read
      const input = await readChat(req, Boolean(pinned));
      const { result, key } = await run(
        "chat",
        route,
        providers,
        requestId,
        deps,
        (k, signal) => chat(k, input, deps.env, deps.fetch, signal),
        (r) => ({ input_tokens: r.input_tokens, output_tokens: r.output_tokens }),
      );
      return reply(200, {
        id: `chatcmpl-${requestId}`,
        object: "chat.completion",
        created: Math.floor(Date.now() / 1000),
        model: result.model,
        provider: key.provider,
        choices: [{
          index: 0,
          message: { role: "assistant", content: result.content },
          finish_reason: result.finish_reason,
        }],
        usage: {
          prompt_tokens: result.input_tokens,
          completion_tokens: result.output_tokens,
          total_tokens: (result.input_tokens ?? 0) + (result.output_tokens ?? 0),
        },
      }, { ...ids, "x-gateway-provider": key.provider, "x-gateway-key-id": key.id });
    }

    const providers = order("audio", pinned?.[1] ?? null, deps);
    const audio = await readAudio(req);
    const { result, key } = await run(
      "audio",
      route,
      providers,
      requestId,
      deps,
      (k, signal) => transcribe(k, audio, deps.env, deps.fetch, signal),
      (r) => ({ audio_seconds: r.duration ?? (r.segments.at(-1)?.end ?? null) }),
    );
    return reply(200, { ...result, provider: key.provider }, {
      ...ids,
      "x-gateway-provider": key.provider,
      "x-gateway-key-id": key.id,
    });
  } catch (err) {
    if (err instanceof GatewayError) {
      return reply(
        err.status,
        { error: { code: err.code, message: err.message, request_id: requestId, ...err.extra } },
        { ...ids, ...err.headers },
      );
    }
    console.error(
      JSON.stringify({
        event: "internal_error",
        request_id: requestId,
        error: redact(String((err as Error)?.stack ?? err), deps),
      }),
    );
    return reply(500, { error: { code: "internal", message: "Internal error", request_id: requestId } }, ids);
  }
}
