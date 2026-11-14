-- AI gateway bookkeeping. Only key ids are stored: the first 12 hex characters of each key's SHA-256.
-- The keys themselves exist only as Edge Function secrets.

create table if not exists public.ai_key_state (
  key_id text primary key,
  provider text not null,
  cooldown_until timestamptz,            -- the key is skipped until then
  reason text,                           -- why it is cooling down
  strikes integer not null default 0,    -- failures in a row; each doubles the next cooldown
  last_used_at timestamptz,
  updated_at timestamptz not null default now()
);

create table if not exists public.ai_usage (
  id bigint generated always as identity primary key,
  at timestamptz not null default now(),
  request_id uuid not null,
  route text not null,
  provider text not null,
  key_id text,
  status integer not null,               -- 200, the provider's error status, or 0 when it never answered
  latency_ms integer not null,
  input_tokens integer,
  output_tokens integer,
  audio_seconds real,
  error text                             -- already redacted by the gateway
);
create index if not exists ai_usage_at on public.ai_usage (at);
create index if not exists ai_usage_key_at on public.ai_usage (key_id, at);

create or replace view public.ai_key_usage_24h with (security_invoker = true) as
select provider, key_id,
       count(*) filter (where status = 200) as ok_24h,
       count(*) filter (where status <> 200) as failed_24h,
       coalesce(sum(coalesce(input_tokens, 0) + coalesce(output_tokens, 0)), 0) as tokens_24h,
       coalesce(sum(audio_seconds), 0) as audio_seconds_24h
from public.ai_usage
where at > now() - interval '24 hours' and key_id is not null
group by provider, key_id;

-- Row level security with no policies: the anon and authenticated roles (anything a browser can hold)
-- read and write nothing. Only the service role, which only the gateway has, bypasses it.
alter table public.ai_key_state enable row level security;
alter table public.ai_usage enable row level security;
revoke all on public.ai_key_state, public.ai_usage, public.ai_key_usage_24h from anon, authenticated;
