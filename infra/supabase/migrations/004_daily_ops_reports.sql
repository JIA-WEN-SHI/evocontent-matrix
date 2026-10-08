-- Daily ops run reports (step 5 v2)
-- Stores scheduler/manual run attempts, status, result and errors.

create table if not exists daily_ops_reports (
  id uuid primary key default gen_random_uuid(),
  domain_slug text not null,
  run_key text not null,
  attempt integer not null default 1,
  triggered_by text not null default 'system:daily_ops',
  status text not null check (status in ('running', 'success', 'failed', 'skipped')),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  result_jsonb jsonb not null default '{}'::jsonb,
  error_message text,
  created_at timestamptz not null default now()
);

create index if not exists idx_daily_ops_reports_domain_started
  on daily_ops_reports (domain_slug, started_at desc);

create index if not exists idx_daily_ops_reports_run_key_attempt
  on daily_ops_reports (run_key, attempt desc);
