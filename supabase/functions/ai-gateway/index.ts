// ClipForge AI gateway: the only place the AI provider keys exist. Deployed as a Supabase Edge Function;
// the keys are Supabase secrets (see docs/AI_GATEWAY.md). Routes and policies are in router.ts.
import { loadKeys } from "./pool.ts";
import { handle } from "./router.ts";
import { ResilientStore, SupabaseStore } from "./store.ts";

const env = (name: string) => Deno.env.get(name);
const pools = await loadKeys(env);
const url = env("SUPABASE_URL");
const serviceKey = env("SUPABASE_SERVICE_ROLE_KEY") ?? env("SUPABASE_SECRET_KEY"); // injected by Supabase
const store = new ResilientStore(url && serviceKey ? new SupabaseStore(url, serviceKey, fetch) : null);

console.log(JSON.stringify({
  event: "gateway_started",
  keys: Object.fromEntries([...pools].map(([provider, keys]) => [provider, keys.map((k) => k.id)])), // ids only
}));

Deno.serve((req) => handle(req, { pools, store, fetch, env, token: env("GATEWAY_TOKEN") }));
