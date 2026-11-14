# AI gateway on Supabase

Every AI provider key lives in one place: Supabase Edge Function secrets. The `ai-gateway` function is the only code that can read them. The ClipForge worker holds one revocable token for the gateway. The browser never calls the gateway and never sees the token or a key.

```
 Browser (React)                 ClipForge worker (Docker)                Supabase
 ───────────────                 ─────────────────────────                ────────────────────────────────────────────
 no keys, no tokens  ──/api/──▶  GATEWAY_URL + GATEWAY_TOKEN  ──HTTPS──▶  Edge Function ai-gateway
 no AI calls                     (no provider keys)                       │  secrets: GEMINI_API_KEYS, GROQ_API_KEYS,
                                 falls back to local Whisper              │  ZAI_API_KEYS, CLOUDFLARE_*, OPENROUTER_API_KEYS
                                 and local titles if the                  │  picks provider → key → calls it
                                 gateway is down                          ▼
                                                                          Postgres (RLS: service role only)
                                                                          ai_key_state  cooldowns, strikes, last use
                                                                          ai_usage      one row per upstream call
                                                                          ai_key_usage_24h  view for /health
                                                                          (key ids only: first 12 hex of SHA-256)
```

## Routes

All routes need `Authorization: Bearer <GATEWAY_TOKEN>`. Base URL: `https://<project-ref>.supabase.co/functions/v1/ai-gateway`.

| Method | Path | Does |
|---|---|---|
| POST | `/v1/chat/completions` | Text in OpenAI's format. Routed over every provider with keys, in `AI_TEXT_ORDER` |
| POST | `/v1/providers/{gemini,groq,zai,cloudflare,openrouter}/chat/completions` | The same, one provider only. `model` is honoured here |
| POST | `/v1/audio/transcriptions` | Speech to text: multipart `file` (max 25 MB) and optional `language`. Groq, then Cloudflare |
| POST | `/v1/providers/{groq,cloudflare}/audio/transcriptions` | The same, one provider only |
| GET | `/v1/health` | Every key's state (ready, cooling, refused), cooldown end, 24-hour calls, tokens and audio seconds. Key ids only |
| POST | `/v1/health/probe` | One tiny request per key, ignoring cooldowns. Use after setup or rotation |

Answers carry `x-request-id`, `x-gateway-provider` and `x-gateway-key-id`. Errors are JSON: `{"error": {"code", "message", "request_id", "attempts"?}}`.

## How keys are divided

**By task.** Each provider does what it is best at for free:

| Task | Order | Why |
|---|---|---|
| Titles, descriptions, hashtags, clip scoring | Gemini → Groq → Z.ai → Cloudflare → OpenRouter | Gemini writes the best Hindi and English; the rest are fallbacks |
| Speech to text | Groq → Cloudflare | Groq Whisper is fast and allows about 8 h of audio a day per key; Cloudflare's daily Neurons cover about 3.5 h |

Change the order with the `AI_TEXT_ORDER` and `AI_SPEECH_ORDER` secrets, for example `groq,gemini`.

**Within a provider.** All of a provider's keys form one pool. Each request takes the key that was used least recently, so traffic spreads evenly and two free keys give about twice the free quota.

**On failure.**

| Upstream answer | Meaning | The key | The request |
|---|---|---|---|
| 429 | Rate limit or free quota spent | Cools for `Retry-After` (or Gemini's `retryDelay`), at least 1 min, doubling with each failure in a row up to 1 h | Next key, then next provider |
| 401, 403 | Key refused | Benched for 24 h, shown as `refused` in `/health` | Next key, then next provider |
| 5xx, timeout, network error | Provider trouble | Cools 30 s, doubling up to 10 min | Next key, then next provider |
| 400, 404, 413, 422 | Our request does not fit this provider | Not blamed | Next provider (other keys would fail the same way) |

A success resets the key's strikes. Each attempt has its own timeout (60 s text, 120 s audio), and the whole request stops trying before Supabase's 150 s limit. If every key is cooling, the answer is 503 with a `Retry-After`. If every attempt failed, it is 502 with the list of attempts, already redacted.

## Security

- Keys exist only as Supabase secrets. They are never in Git, in the worker's `.env`, in the database, in logs or in any answer. Logs, `/health`, and the database use key ids: the first 12 hex characters of the key's SHA-256.
- Upstream error text is redacted: every configured key and the gateway token are removed before it is logged or returned, because some APIs echo the key they refused.
- The gateway token is compared in constant time (SHA-256 digests). Without `GATEWAY_TOKEN` set, the gateway refuses everything.
- Any request with an `Origin` header (every browser request across sites) is refused, so the gateway cannot be used from a web page even if the token leaked.
- `verify_jwt = false` for this function on purpose: Supabase's anon JWT is public and proves nothing, so the gateway checks its own token.
- The tables have row level security with no policies, and anon and authenticated roles have no grants. Only the service role, injected into the function by Supabase, can read them.
- Input limits: 512 KB per text request, 50 messages, 8,192 output tokens, 25 MB of audio, language codes checked.
- If the database is unreachable, the gateway keeps working with cooldowns held in memory.

## Setup on Windows

You need a free Supabase account, a free project, and Node.js for the Supabase CLI (`winget install OpenJS.NodeJS.LTS`). Run everything from the `clipforge` folder.

1. Log in and link the project. The project ref is in the dashboard URL, `supabase.com/dashboard/project/<ref>`.
   ```bash
   npx supabase@latest login
   ```
   ```bash
   npx supabase@latest link --project-ref <your-project-ref>
   ```
2. Create the tables:
   ```bash
   npx supabase@latest db push
   ```
3. Make a gateway token with a secure random generator:
   ```bash
   docker compose exec clipforge python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
4. Open Notepad and save a file `supabase\.env.secrets` (Git ignores it) with your keys, one secret per line, several keys separated by commas:
   ```
   GATEWAY_TOKEN=<the token from step 3>
   GEMINI_API_KEYS=<gemini key 1>,<gemini key 2>
   GROQ_API_KEYS=<groq key 1>,<groq key 2>
   ZAI_API_KEYS=<z.ai key>
   ```
   Optional: `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKENS`, `OPENROUTER_API_KEYS`, model overrides such as `GROQ_MODEL`.
5. Upload the secrets, then delete the file:
   ```bash
   npx supabase@latest secrets set --env-file supabase/.env.secrets
   ```
   ```bash
   del supabase\.env.secrets
   ```
6. Deploy the function:
   ```bash
   npx supabase@latest functions deploy ai-gateway --no-verify-jwt
   ```
7. In ClipForge's `.env`, remove any provider keys and add:
   ```
   GATEWAY_URL=https://<your-project-ref>.supabase.co/functions/v1/ai-gateway
   GATEWAY_TOKEN=<the same token>
   TRANSCRIBE_PROVIDERS=gateway,local
   ```
8. Restart and test every key with one tiny request each:
   ```bash
   docker compose up -d
   ```
   ```bash
   docker compose exec clipforge python -m app.ai
   ```

## Rotating a key

1. Create the new key in the provider's console.
2. Put both old and new keys in the secret, for example `GROQ_API_KEYS=<old>,<new>`, and run `secrets set` again (step 5).
3. Run `python -m app.ai` to check the new key.
4. Remove the old key from the secret, set it again, and delete the old key in the provider's console.

To rotate the gateway token, set a new `GATEWAY_TOKEN` secret and put the same value in ClipForge's `.env`.

## Free limits

| Service | Free allowance |
|---|---|
| Supabase | 500,000 Edge Function calls a month, 500 MB database, 150 s per call. Free projects pause after a week without activity; ClipForge then falls back to local models until you restore the project in the dashboard |
| Gemini | Set per project in AI Studio; changed several times since 2025 |
| Groq Whisper | About 8 h of audio a day per key, 25 MB per file |
| Z.ai `glm-4.5-flash` | Free, rate limited |
| Cloudflare Workers AI | 10,000 Neurons a day per account |
| OpenRouter `:free` | 50 requests a day |

A typical video makes one text call, plus one speech call per 10 minutes of audio when cloud speech is on.
