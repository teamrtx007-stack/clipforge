// deno test supabase/functions/ai-gateway — no network: every provider is a fake.
import { loadKeys } from "./pool.ts";
import { type Deps, handle } from "./router.ts";
import { MemoryStore, ResilientStore, type Store } from "./store.ts";

const SECRETS: Record<string, string> = {
  GEMINI_API_KEYS: "AQ.gemini-one,AQ.gemini-two",
  GROQ_API_KEYS: "gsk_groq-one\ngsk_groq-two",
  ZAI_API_KEYS: "zai.secret-one",
  CLOUDFLARE_ACCOUNT_ID: "acct123",
  CLOUDFLARE_API_TOKENS: "cf-token-one",
};
const TOKEN = "gateway-token-for-tests";

function assert(cond: unknown, msg = "assertion failed"): asserts cond {
  if (!cond) throw new Error(msg);
}
function eq<T>(a: T, b: T, msg = "") {
  if (JSON.stringify(a) !== JSON.stringify(b)) {
    throw new Error(`${msg} expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
  }
}

type Reply = { status: number; body: unknown; headers?: Record<string, string> };
interface Call {
  url: string;
  init: RequestInit;
  auth: string;
}

/** A fake internet: each provider answers from a queue, or with a default success. */
function fakeNet(scripts: Record<string, Reply[]> = {}) {
  const calls: Call[] = [];
  const fetch = async (url: string, init: RequestInit): Promise<Response> => {
    const headers = new Headers(init.headers);
    const auth = headers.get("authorization") ?? headers.get("x-goog-api-key") ?? "";
    calls.push({ url, init, auth });
    const provider = url.includes("googleapis")
      ? "gemini"
      : url.includes("groq")
      ? "groq"
      : url.includes("z.ai")
      ? "zai"
      : url.includes("cloudflare")
      ? "cloudflare"
      : "openrouter";
    const r = scripts[provider]?.shift() ?? success(provider, url);
    return new Response(typeof r.body === "string" ? r.body : JSON.stringify(r.body), {
      status: r.status,
      headers: r.headers,
    });
  };
  return { fetch, calls };
}

function success(provider: string, url: string): Reply {
  if (url.includes("audio/transcriptions")) {
    return {
      status: 200,
      body: {
        language: "hi",
        duration: 3,
        segments: [{ start: 0, end: 2, text: " नमस्ते" }],
        words: [{ word: "नमस्ते", start: 0.1, end: 0.8 }],
      },
    };
  }
  if (url.includes("/ai/run/")) {
    return {
      status: 200,
      body: {
        success: true,
        result: {
          transcription_info: { language: "en" },
          segments: [{ start: 0, end: 1, text: "hi", words: [{ word: "hi", start: 0, end: 0.5 }] }],
        },
      },
    };
  }
  if (provider === "gemini") {
    return {
      status: 200,
      body: {
        candidates: [{
          content: { parts: [{ text: "thinking...", thought: true }, { text: '{"ok":true}' }] },
          finishReason: "STOP",
        }],
        usageMetadata: { promptTokenCount: 5, candidatesTokenCount: 3 },
      },
    };
  }
  return {
    status: 200,
    body: {
      model: `${provider}-model`,
      choices: [{ message: { content: `${provider} says hi` }, finish_reason: "stop" }],
      usage: { prompt_tokens: 4, completion_tokens: 2 },
    },
  };
}

async function gateway(
  net = fakeNet(),
  store: Store = new MemoryStore(),
  secrets = SECRETS,
  now?: () => number,
): Promise<Deps & { calls: Call[] }> {
  const env = (n: string) => secrets[n];
  return { pools: await loadKeys(env), store, fetch: net.fetch, env, token: TOKEN, now, calls: net.calls };
}

function req(path: string, body?: unknown, headers: Record<string, string> = {}, method = "POST"): Request {
  return new Request(`http://localhost/ai-gateway${path}`, {
    method,
    headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json", ...headers },
    body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
  });
}

const CHAT = {
  messages: [{ role: "system", content: "Be brief." }, { role: "user", content: "hello" }],
  max_tokens: 100,
};

Deno.test("refuses missing tokens, browsers, and an unconfigured gateway", async () => {
  const deps = await gateway();
  eq((await handle(req("/v1/health", undefined, { authorization: "Bearer wrong" }, "GET"), deps)).status, 401);
  eq((await handle(req("/v1/health", undefined, { origin: "https://evil.example" }, "GET"), deps)).status, 403);
  eq((await handle(req("/v1/health", undefined, {}, "GET"), { ...deps, token: undefined })).status, 503);
  eq(deps.calls.length, 0, "no provider was called");
});

Deno.test("validates input and routes", async () => {
  const deps = await gateway();
  eq((await handle(req("/v1/chat/completions", { messages: [] }), deps)).status, 400);
  eq((await handle(req("/v1/chat/completions", { ...CHAT, max_tokens: 99999 }), deps)).status, 400);
  eq(
    (await handle(req("/v1/chat/completions", { messages: [{ role: "user", content: "x".repeat(600_000) }] }), deps))
      .status,
    413,
  );
  eq((await handle(req("/v1/providers/nope/chat/completions", CHAT), deps)).status, 404);
  eq((await handle(req("/v1/providers/zai/audio/transcriptions", CHAT), deps)).status, 404);
  eq((await handle(req("/v1/chat/completions", undefined, {}, "GET"), deps)).status, 405);
  eq((await handle(req("/v1/whatever", CHAT), deps)).status, 404);
  eq(deps.calls.length, 0);
});

Deno.test("gemini goes first, through its native API, and thinking text is dropped", async () => {
  const deps = await gateway();
  const res = await handle(req("/v1/chat/completions", { ...CHAT, response_format: { type: "json_object" } }), deps);
  const body = await res.json();
  eq(res.status, 200);
  eq(body.provider, "gemini");
  eq(body.choices[0].message.content, '{"ok":true}');
  const call = deps.calls[0];
  assert(call.url.includes("/models/gemini-2.5-flash:generateContent"));
  assert(new Headers(call.init.headers).get("x-goog-api-key")?.startsWith("AQ."), "AQ. keys go in x-goog-api-key");
  assert(!new Headers(call.init.headers).has("authorization"));
  const sent = JSON.parse(call.init.body as string);
  eq(sent.systemInstruction.parts[0].text, "Be brief.");
  eq(sent.generationConfig.responseMimeType, "application/json");
});

Deno.test("keys of one provider take turns", async () => {
  const deps = await gateway();
  let t = 1_000_000;
  deps.now = () => (t += 1000);
  for (let i = 0; i < 4; i++) eq((await handle(req("/v1/providers/groq/chat/completions", CHAT), deps)).status, 200);
  eq(deps.calls.map((c) => c.auth), [
    "Bearer gsk_groq-one",
    "Bearer gsk_groq-two",
    "Bearer gsk_groq-one",
    "Bearer gsk_groq-two",
  ]);
});

Deno.test("a spent quota cools the key, the next key and provider take over, and later requests skip it", async () => {
  const net = fakeNet({
    gemini: [
      { status: 429, body: { error: { details: [{ retryDelay: "40s" }] } } },
      { status: 429, body: "quota exceeded" },
    ],
  });
  const deps = await gateway(net);
  const first = await handle(req("/v1/chat/completions", CHAT), deps);
  eq((await first.json()).provider, "groq", "after both Gemini keys hit 429");
  eq(net.calls.length, 3);
  const second = await handle(req("/v1/chat/completions", CHAT), deps);
  eq((await second.json()).provider, "groq");
  eq(net.calls.length, 4, "cooling Gemini keys are not tried again");
  const health = await (await handle(req("/v1/health", undefined, {}, "GET"), deps)).json();
  eq(health.providers.gemini.keys.map((k: any) => k.state), ["cooling", "cooling"]);
});

Deno.test("a refused key is benched for a day and shows as refused", async () => {
  const net = fakeNet({ zai: [{ status: 401, body: { error: "invalid key zai.secret-one" } }] });
  const deps = await gateway(net);
  const res = await handle(req("/v1/providers/zai/chat/completions", CHAT), deps);
  eq(res.status, 502);
  const body = await res.json();
  eq(body.error.code, "all_providers_failed");
  assert(!JSON.stringify(body).includes("zai.secret-one"), "an echoed key is redacted");
  assert(JSON.stringify(body).includes("[redacted]"));
  eq(res.headers.get("retry-after"), "86400");
  const again = await handle(req("/v1/providers/zai/chat/completions", CHAT), deps);
  eq(again.status, 503, "no usable key left");
  eq((await again.json()).error.code, "all_keys_cooling");
  const health = await (await handle(req("/v1/health", undefined, {}, "GET"), deps)).json();
  eq(health.providers.zai.keys[0].state, "refused");
});

Deno.test("a bad request skips to the next provider without blaming the key", async () => {
  const net = fakeNet({ gemini: [{ status: 400, body: "unsupported parameter" }] });
  const deps = await gateway(net);
  eq((await (await handle(req("/v1/chat/completions", CHAT), deps)).json()).provider, "groq");
  eq(net.calls.length, 2, "the second Gemini key is not tried with the same bad request");
  const health = await (await handle(req("/v1/health", undefined, {}, "GET"), deps)).json();
  eq(health.providers.gemini.keys.map((k: any) => k.state), ["ready", "ready"]);
});

Deno.test("speech to text: Groq first, Cloudflare when Groq is out of quota", async () => {
  const net = fakeNet({
    groq: [{ status: 429, body: "", headers: { "retry-after": "3600" } }, { status: 429, body: "" }],
  });
  const deps = await gateway(net);
  const form = new FormData();
  form.append("file", new Blob([new Uint8Array([1, 2, 3])], { type: "audio/mpeg" }), "chunk.mp3");
  form.append("language", "hi");
  const res = await handle(
    new Request("http://localhost/ai-gateway/v1/audio/transcriptions", {
      method: "POST",
      headers: { authorization: `Bearer ${TOKEN}` },
      body: form,
    }),
    deps,
  );
  const body = await res.json();
  eq(res.status, 200);
  eq(body.provider, "cloudflare");
  eq(body.segments[0].words[0].word, "hi");
  const cf = JSON.parse(net.calls[2].init.body as string);
  eq(cf.audio, "AQID");
  eq(cf.language, "hi");
  assert(net.calls[2].url.endsWith("/accounts/acct123/ai/run/@cf/openai/whisper-large-v3-turbo"));
});

Deno.test("groq speech request and answer shape", async () => {
  const deps = await gateway();
  const form = new FormData();
  form.append("file", new Blob([new Uint8Array([9])]), "a.mp3");
  const body = await (await handle(
    new Request("http://localhost/ai-gateway/v1/providers/groq/audio/transcriptions", {
      method: "POST",
      headers: { authorization: `Bearer ${TOKEN}` },
      body: form,
    }),
    deps,
  )).json();
  eq(body.segments, [{ start: 0, end: 2, text: "नमस्ते", words: [{ word: "नमस्ते", start: 0.1, end: 0.8 }] }]);
  const sent = deps.calls[0].init.body as FormData;
  eq(sent.get("model"), "whisper-large-v3-turbo");
  eq(sent.getAll("timestamp_granularities[]"), ["segment", "word"]);
});

Deno.test("health and probe never contain a secret", async () => {
  const deps = await gateway(fakeNet({ groq: [{ status: 403, body: "key gsk_groq-one is disabled" }] }));
  const health = await (await handle(req("/v1/health", undefined, {}, "GET"), deps)).text();
  const probe = await (await handle(req("/v1/health/probe"), deps)).json();
  for (const text of [health, JSON.stringify(probe)]) {
    for (const secret of Object.values(SECRETS).join(",").split(/[,\n]/)) {
      assert(secret === "acct123" || !text.includes(secret), `leaked ${secret}`);
    }
  }
  eq(probe.results.length, 6, "every key of every text provider was probed");
  eq(probe.results.filter((r: any) => !r.ok).map((r: any) => r.status), [403]);
});

Deno.test("a database outage does not stop requests", async () => {
  const broken: Store = {
    states: () => Promise.reject(new Error("db down")),
    save: () => Promise.reject(new Error("db down")),
    log: () => Promise.reject(new Error("db down")),
    summary: () => Promise.reject(new Error("db down")),
  };
  const deps = await gateway(fakeNet(), new ResilientStore(broken));
  eq((await handle(req("/v1/chat/completions", CHAT), deps)).status, 200);
  eq((await handle(req("/v1/health", undefined, {}, "GET"), deps)).status, 200);
});
