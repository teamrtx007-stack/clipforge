// One adapter per provider. Each turns the gateway's request into the provider's own API call and the
// answer back into one shape. Nothing here chooses keys or retries: the router does that.
import type { Key, Provider } from "./pool.ts";

export interface Message {
  role: "system" | "user" | "assistant";
  content: string;
}
export interface ChatInput {
  messages: Message[];
  max_tokens: number;
  temperature: number;
  json: boolean;
  model?: string;
}
export interface ChatOutput {
  content: string;
  model: string;
  finish_reason: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
}
export interface Word {
  word: string;
  start: number;
  end: number;
}
export interface Segment {
  start: number;
  end: number;
  text: string;
  words: Word[];
}
export interface Transcript {
  language: string | null;
  duration: number | null;
  segments: Segment[];
  model: string;
}
export interface AudioInput {
  file: Blob;
  filename: string;
  language?: string;
}

export type Fetch = (url: string, init: RequestInit) => Promise<Response>;
type Env = (name: string) => string | undefined;

/** A provider call that did not succeed. Status 0 means it never answered (network error or timeout). */
export class UpstreamError extends Error {
  constructor(readonly status: number, message: string, readonly retryAfterMs: number | null = null) {
    super(message);
  }
}

// Model IDs checked November 2026. Each can be overridden with a secret, e.g. GROQ_MODEL or GROQ_WHISPER_MODEL.
export const DEFAULT_MODELS = {
  text: {
    gemini: "gemini-3.8-flash", // gemini-2.5-flash is closed to new accounts
    groq: "openai/gpt-oss-120b",
    ollama: "gemma4:31b", // Ollama Cloud; list current names with GET https://ollama.com/api/tags
    cloudflare: "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    openrouter: "openrouter/free",
  } as Record<Provider, string>,
  speech: { groq: "whisper-large-v3-turbo", cloudflare: "@cf/openai/whisper-large-v3-turbo" } as Partial<
    Record<Provider, string>
  >,
};

export const TEXT_PROVIDERS = Object.keys(DEFAULT_MODELS.text) as Provider[];
export const SPEECH_PROVIDERS = Object.keys(DEFAULT_MODELS.speech) as Provider[];

export function modelFor(kind: "text" | "speech", provider: Provider, env: Env): string {
  const name = `${provider.toUpperCase()}_${kind === "text" ? "MODEL" : "WHISPER_MODEL"}`;
  return env(name) || DEFAULT_MODELS[kind][provider] || "";
}

function retryAfter(res: Response, body: string): number | null {
  const header = res.headers.get("retry-after");
  if (header) {
    const seconds = Number(header);
    if (Number.isFinite(seconds)) return seconds * 1000;
    const date = Date.parse(header);
    if (!Number.isNaN(date)) return Math.max(0, date - Date.now());
  }
  const gemini = body.match(/"retryDelay":\s*"(\d+(?:\.\d+)?)s"/); // Gemini puts it in the error body
  return gemini ? Number(gemini[1]) * 1000 : null;
}

async function call(fetch: Fetch, url: string, init: RequestInit): Promise<any> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err) {
    const timedOut = (err as Error)?.name === "TimeoutError" || (err as Error)?.name === "AbortError";
    throw new UpstreamError(0, timedOut ? "timed out" : "could not connect");
  }
  const text = await res.text();
  if (!res.ok) throw new UpstreamError(res.status, `HTTP ${res.status}: ${text.slice(0, 500)}`, retryAfter(res, text));
  try {
    return JSON.parse(text);
  } catch {
    throw new UpstreamError(502, "the answer was not JSON");
  }
}

const OPENAI_CHAT: Partial<Record<Provider, (k: Key, env: Env) => string>> = {
  groq: () => "https://api.groq.com/openai/v1/chat/completions",
  ollama: () => "https://ollama.com/v1/chat/completions",
  openrouter: () => "https://openrouter.ai/api/v1/chat/completions",
  cloudflare: (k) => `https://api.cloudflare.com/client/v4/accounts/${k.account}/ai/v1/chat/completions`,
};

async function geminiChat(
  key: Key,
  input: ChatInput,
  model: string,
  fetch: Fetch,
  signal: AbortSignal,
): Promise<ChatOutput> {
  // The native API, because new "AQ." Gemini keys are refused by the OpenAI-compatible endpoint.
  const system = input.messages.filter((m) => m.role === "system").map((m) => m.content).join("\n\n");
  const body = {
    ...(system ? { systemInstruction: { parts: [{ text: system }] } } : {}),
    contents: input.messages.filter((m) => m.role !== "system")
      .map((m) => ({ role: m.role === "assistant" ? "model" : "user", parts: [{ text: m.content }] })),
    generationConfig: {
      temperature: input.temperature,
      maxOutputTokens: input.max_tokens,
      ...(input.json ? { responseMimeType: "application/json" } : {}),
      // Thinking tokens count against maxOutputTokens; a small budget keeps the answer from being cut off.
      ...(model.startsWith("gemini-2.5")
        ? { thinkingConfig: { thinkingBudget: 512 } }
        : model.startsWith("gemini-3")
        ? { thinkingConfig: { thinkingLevel: "low" } }
        : {}),
    },
  };
  const data = await call(
    fetch,
    `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`,
    {
      method: "POST",
      signal,
      body: JSON.stringify(body),
      headers: { "content-type": "application/json", "x-goog-api-key": key.secret },
    },
  );
  const candidate = data?.candidates?.[0];
  if (!candidate) throw new UpstreamError(422, `no answer: ${data?.promptFeedback?.blockReason ?? "empty"}`);
  const content = (candidate.content?.parts ?? []).filter((p: any) => !p.thought).map((p: any) => p.text ?? "").join(
    "",
  );
  if (!content.trim()) throw new UpstreamError(502, `empty answer (${candidate.finishReason ?? "unknown"})`);
  return {
    content,
    model,
    finish_reason: candidate.finishReason ?? null,
    input_tokens: data.usageMetadata?.promptTokenCount ?? null,
    output_tokens: data.usageMetadata?.candidatesTokenCount ?? null,
  };
}

export async function chat(
  key: Key,
  input: ChatInput,
  env: Env,
  fetch: Fetch,
  signal: AbortSignal,
): Promise<ChatOutput> {
  const model = input.model || modelFor("text", key.provider, env);
  if (key.provider === "gemini") return geminiChat(key, input, model, fetch, signal);
  const body: Record<string, unknown> = {
    model,
    messages: input.messages,
    max_tokens: input.max_tokens,
    temperature: input.temperature,
  };
  // Cloudflare and Ollama Cloud models do not take a forced JSON format; the prompt asks for JSON instead.
  if (input.json && !["cloudflare", "ollama"].includes(key.provider)) body.response_format = { type: "json_object" };
  if (model.startsWith("openai/gpt-oss")) body.reasoning_effort = "low"; // a reasoning model; keep it short
  const data = await call(fetch, OPENAI_CHAT[key.provider]!(key, env), {
    method: "POST",
    signal,
    body: JSON.stringify(body),
    headers: { "content-type": "application/json", authorization: `Bearer ${key.secret}` },
  });
  const choice = data?.choices?.[0];
  const content = choice?.message?.content;
  if (typeof content !== "string" || !content.trim()) throw new UpstreamError(502, "empty answer");
  return {
    content,
    model: data.model ?? model,
    finish_reason: choice.finish_reason ?? null,
    input_tokens: data.usage?.prompt_tokens ?? null,
    output_tokens: data.usage?.completion_tokens ?? null,
  };
}

function words(items: any, offset = 0): Word[] {
  return (Array.isArray(items) ? items : [])
    .filter((w) => String(w?.word ?? "").trim())
    .map((w) => ({ word: String(w.word).trim(), start: Number(w.start) + offset, end: Number(w.end) + offset }));
}

function segments(raw: any, topWords: Word[] | null): Segment[] {
  return (Array.isArray(raw) ? raw : [])
    .map((s) => {
      const start = Number(s.start), end = Number(s.end);
      return {
        start,
        end,
        text: String(s.text ?? "").trim(),
        words: topWords ? topWords.filter((w) => w.start >= start && w.start < end) : words(s.words),
      };
    })
    .filter((s) => s.text);
}

function base64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

export async function transcribe(
  key: Key,
  input: AudioInput,
  env: Env,
  fetch: Fetch,
  signal: AbortSignal,
): Promise<Transcript> {
  const model = modelFor("speech", key.provider, env);
  if (key.provider === "groq") {
    const form = new FormData();
    form.append("file", input.file, input.filename);
    form.append("model", model);
    form.append("response_format", "verbose_json");
    form.append("temperature", "0");
    form.append("timestamp_granularities[]", "segment");
    form.append("timestamp_granularities[]", "word");
    if (input.language) form.append("language", input.language);
    const data = await call(fetch, "https://api.groq.com/openai/v1/audio/transcriptions", {
      method: "POST",
      signal,
      body: form,
      headers: { authorization: `Bearer ${key.secret}` },
    });
    if (!Array.isArray(data?.segments)) throw new UpstreamError(502, "the answer had no segments");
    return {
      language: data.language ?? null,
      duration: data.duration ?? null,
      segments: segments(data.segments, words(data.words)),
      model,
    };
  }
  if (key.provider === "cloudflare") {
    const body = {
      audio: base64(new Uint8Array(await input.file.arrayBuffer())),
      vad_filter: true,
      ...(input.language ? { language: input.language } : {}),
    };
    const data = await call(fetch, `https://api.cloudflare.com/client/v4/accounts/${key.account}/ai/run/${model}`, {
      method: "POST",
      signal,
      body: JSON.stringify(body),
      headers: { "content-type": "application/json", authorization: `Bearer ${key.secret}` },
    });
    const result = data?.success === false ? null : data?.result;
    if (!Array.isArray(result?.segments)) {
      throw new UpstreamError(502, `unexpected answer: ${JSON.stringify(data?.errors ?? data).slice(0, 300)}`);
    }
    return {
      language: result.transcription_info?.language ?? null,
      duration: result.transcription_info?.duration ?? null,
      segments: segments(result.segments, null),
      model,
    };
  }
  throw new UpstreamError(400, `${key.provider} has no speech to text`);
}
