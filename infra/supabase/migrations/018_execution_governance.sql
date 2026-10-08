-- Execution governance baseline:
-- 1) durable coach action lifecycle
-- 2) unified action run ledger for tool execution traceability

create extension if not exists pgcrypto;

create table if not exists coach_actions (
  id uuid primary key default gen_random_uuid(),
  action_id text not null unique,
  domain_id uuid references domains(id) on delete cascade,
  domain_slug text not null default '',
  account_id uuid references channel_accounts(id) on delete set null,
  triggered_by text not null default 'api:coach',
  message text not null default '',
  action_jsonb jsonb not null default '{}'::jsonb,
  brief_jsonb jsonb not null default '{}'::jsonb,
  context_jsonb jsonb not null default '{}'::jsonb,
  status text not null default 'suggested' check (
    status in (
      'suggested',
      'confirmed',
      'running',
      'completed',
      'retry_later',
      'failed',
      'canceled',
      'expired'
    )
  ),
  confirmed_by text not null default '',
  confirmed_at timestamptz,
  executed_at timestamptz,
  result_jsonb jsonb not null default '{}'::jsonb,
  error_code text not null default '',
  error_message text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists action_runs (
  id uuid primary key default gen_random_uuid(),
  run_key text not null unique,
  domain_id uuid references domains(id) on delete cascade,
  domain_slug text not null default '',
  account_id uuid references channel_accounts(id) on delete set null,
  actor text not null default '',
  source text not null default '',
  source_ref text not null default '',
  action_type text not null default '',
  idempotency_key text not null default '',
  trace_id text not null default '',
  policy_snapshot jsonb not null default '{}'::jsonb,
  input_jsonb jsonb not null default '{}'::jsonb,
  result_jsonb jsonb not null default '{}'::jsonb,
  status text not null default 'queued' check (
    status in ('queued', 'running', 'success', 'failed', 'retry_later', 'blocked', 'canceled')
  ),
  retryable boolean not null default false,
  error_code text not null default '',
  error_message text not null default '',
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_coach_actions_domain_status_created
  on coach_actions (domain_slug, status, created_at desc);

create index if not exists idx_coach_actions_account_status_created
  on coach_actions (account_id, status, created_at desc);

create index if not exists idx_action_runs_domain_created
  on action_runs (domain_slug, created_at desc);

create index if not exists idx_action_runs_status_created
  on action_runs (status, created_at desc);

create index if not exists idx_action_runs_trace
  on action_runs (trace_id, created_at desc);

do $$
begin
  if exists (select 1 from pg_proc where proname = 'kb_touch_updated_at') then
    if not exists (select 1 from pg_trigger where tgname = 'trg_coach_actions_touch_updated_at') then
      create trigger trg_coach_actions_touch_updated_at before update on coach_actions
        for each row execute function kb_touch_updated_at();
    end if;
    if not exists (select 1 from pg_trigger where tgname = 'trg_action_runs_touch_updated_at') then
      create trigger trg_action_runs_touch_updated_at before update on action_runs
        for each row execute function kb_touch_updated_at();
    end if;
  end if;
end $$;

do $$
declare
  tbl text;
  tables text[] := array['coach_actions', 'action_runs'];
begin
  foreach tbl in array tables loop
    if exists (
      select 1 from information_schema.tables
      where table_schema = 'public' and table_name = tbl
    ) then
      execute format('alter table %I enable row level security;', tbl);

      execute format('drop policy if exists %I on %I;', tbl || '_service_role_all', tbl);
      execute format('create policy %I on %I for all to service_role using (true) with check (true);', tbl || '_service_role_all', tbl);

      execute format('drop policy if exists %I on %I;', tbl || '_anon_deny_all', tbl);
      execute format('create policy %I on %I for all to anon using (false) with check (false);', tbl || '_anon_deny_all', tbl);
    end if;
  end loop;
end $$;
