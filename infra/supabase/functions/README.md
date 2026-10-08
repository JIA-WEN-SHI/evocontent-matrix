# Supabase Edge Functions (RPA Orchestration)

This folder contains the V1 orchestration chain:

- `rpa-dispatch`: receive LLM task instruction, validate template, dispatch to Octopus API.
- `octopus-callback`: verify callback signature (`HMAC-SHA256 + timestamp`), normalize/clean, store raw+clean rows, and trigger acceptance.
- `kb-accept`: manual/internal accept/replay endpoint for a specific `run_id`.

## Required Secrets

Set in Supabase Edge Function secrets:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `OCTOPUS_API_BASE_URL`
- `OCTOPUS_API_KEY` (optional if Octopus endpoint does not require it)
- `OCTOPUS_API_SECRET` (optional request signing for dispatch)
- `OCTOPUS_CALLBACK_SECRET` (required for callback verification)
- `OCTOPUS_CALLBACK_URL` (public URL of `octopus-callback` function)

## Deploy

```bash
supabase functions deploy rpa-dispatch
supabase functions deploy kb-accept
supabase functions deploy octopus-callback --no-verify-jwt
```

`octopus-callback` typically needs `--no-verify-jwt` for external webhook access.
