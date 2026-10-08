-- Daily closed-loop run ledger (account-aware)
-- Adds explicit run bookkeeping + evidence/decision records + memory items.

create table if not exists runs_daily (
  id uuid primary key default gen_random_uuid(),
  run_key text not null unique,
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  flow text not null default 'full' check (flow in ('full', 'viewpoint')),
  run_date date not null default (now() at time zone 'utc')::date,
  status text not null default 'CREATED' check (
    status in (
      'CREATED',
      'COLLECTING',
      'COLLECTED',
      'EVIDENCE_READY',
      'PLANNING',
      'DRAFTED',
      'AWAITING_APPROVAL',
      'PUBLISHING',
      'PUBLISHED',
      'RETRO_DOING',
      'RETRO_DONE',
      'FAILED'
    )
  ),
  target_posts_min integer not null default 1,
  evidence_pack jsonb not null default '{}'::jsonb,
  llm_plan jsonb not null default '{}'::jsonb,
  retro_report text,
  result_jsonb jsonb not null default '{}'::jsonb,
  error_message text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_runs_daily_domain_date
  on runs_daily (domain_id, run_date desc, created_at desc);

create index if not exists idx_runs_daily_account_date
  on runs_daily (account_id, run_date desc, created_at desc);

create table if not exists decisions (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references runs_daily(id) on delete cascade,
  pipeline_task_id uuid references pipeline_tasks(id) on delete set null,
  evidence_pack jsonb not null default '{}'::jsonb,
  llm_output jsonb not null default '{}'::jsonb,
  summary_text text,
  created_at timestamptz not null default now()
);

create index if not exists idx_decisions_run_created
  on decisions (run_id, created_at desc);

create index if not exists idx_decisions_task_created
  on decisions (pipeline_task_id, created_at desc);

create table if not exists memory_items (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  source_run_id uuid references runs_daily(id) on delete set null,
  type text not null check (type in ('brand_preference', 'strategy_rule', 'asset')),
  title text not null,
  content text not null,
  tags text[] not null default '{}'::text[],
  status text not null default 'active' check (status in ('active', 'pending', 'deprecated')),
  confidence numeric(5,4) not null default 0,
  created_by text not null default 'system',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_memory_items_domain_account_status
  on memory_items (domain_id, account_id, status, updated_at desc);

create index if not exists idx_memory_items_type_status
  on memory_items (type, status, updated_at desc);

