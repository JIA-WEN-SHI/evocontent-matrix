-- Knowledge base ingest + AI job tracking

create extension if not exists pgcrypto;

create table if not exists ingestion_logs (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  source text not null default 'manual',
  source_run_id text not null default '',
  entity_type text not null check (entity_type in ('case', 'asset', 'user_need', 'review')),
  status text not null default 'received' check (status in ('received', 'processing', 'success', 'partial_failed', 'failed')),
  request_payload jsonb not null default '{}'::jsonb,
  normalized_count integer not null default 0,
  success_count integer not null default 0,
  failed_count integer not null default 0,
  error_message text,
  created_by text not null default 'system',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists ai_jobs (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  job_type text not null,
  status text not null default 'queued' check (status in ('queued', 'running', 'success', 'failed', 'canceled')),
  input_jsonb jsonb not null default '{}'::jsonb,
  prompt_version text not null default '',
  model_name text not null default '',
  output_jsonb jsonb not null default '{}'::jsonb,
  error_message text,
  retries integer not null default 0,
  started_at timestamptz,
  finished_at timestamptz,
  source_type text not null default 'system',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_ingestion_logs_domain_entity_created
  on ingestion_logs (domain_id, entity_type, created_at desc);

create index if not exists idx_ingestion_logs_source_run
  on ingestion_logs (source, source_run_id, created_at desc);

create index if not exists idx_ai_jobs_status_type_created
  on ai_jobs (status, job_type, created_at desc)
  where deleted_at is null;

create index if not exists idx_ai_jobs_domain_account_created
  on ai_jobs (domain_id, account_id, created_at desc)
  where deleted_at is null;

do $$
begin
  if not exists (select 1 from pg_trigger where tgname = 'trg_ingestion_logs_touch_updated_at') then
    create trigger trg_ingestion_logs_touch_updated_at before update on ingestion_logs
      for each row execute function kb_touch_updated_at();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'trg_ai_jobs_touch_updated_at') then
    create trigger trg_ai_jobs_touch_updated_at before update on ai_jobs
      for each row execute function kb_touch_updated_at();
  end if;
end $$;
