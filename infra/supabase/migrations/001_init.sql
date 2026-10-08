-- EvoContent Matrix initial schema
create extension if not exists pgcrypto;
create extension if not exists vector;

do $$
begin
  if not exists (select 1 from pg_type where typname = 'task_status') then
    create type task_status as enum (
      'queued',
      'intel_ready',
      'drafting',
      'pending_review',
      'approved',
      'review_rejected',
      'publishing',
      'published',
      'publish_failed',
      'metrics_ready',
      'reflecting',
      'reflection_failed',
      'done'
    );
  end if;
end$$;

create table if not exists domains (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  name text not null,
  config_jsonb jsonb not null default '{}'::jsonb,
  active_strategy_version_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists strategy_versions (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  version integer not null,
  prompt_jsonb jsonb not null default '{}'::jsonb,
  reason text not null,
  created_by text not null default 'system',
  created_at timestamptz not null default now(),
  unique (domain_id, version)
);

alter table domains
  add constraint fk_active_strategy
  foreign key (active_strategy_version_id)
  references strategy_versions(id)
  on delete set null;

create table if not exists tasks (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  channel text not null check (channel in ('xiaohongshu', 'wechat_mp')),
  status task_status not null default 'queued',
  title text,
  body text,
  utm_code text,
  form_id text,
  scheduled_at timestamptz,
  published_at timestamptz,
  leads_count integer not null default 0,
  meta_jsonb jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists intelligence_items (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  source_type text not null,
  source_url text,
  captured_at timestamptz not null default now(),
  raw_text text not null,
  embedding vector(1536),
  meta_jsonb jsonb not null default '{}'::jsonb
);

create table if not exists content_assets (
  id uuid primary key default gen_random_uuid(),
  task_id uuid not null references tasks(id) on delete cascade,
  asset_type text not null,
  uri text,
  prompt text,
  meta_jsonb jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists publishing_records (
  id uuid primary key default gen_random_uuid(),
  task_id uuid not null references tasks(id) on delete cascade,
  channel text not null,
  status text not null,
  remote_post_id text,
  published_url text,
  error_message text,
  meta_jsonb jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists lead_events (
  id uuid primary key default gen_random_uuid(),
  task_id uuid references tasks(id) on delete set null,
  channel text not null,
  utm_code text not null,
  form_id text not null,
  event_time timestamptz not null,
  contact_fields jsonb not null default '{}'::jsonb,
  meta_jsonb jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists audit_logs (
  id uuid primary key default gen_random_uuid(),
  actor text not null,
  action text not null,
  target_type text not null,
  target_id text not null,
  diff_jsonb jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_tasks_status_channel_created
  on tasks (status, channel, created_at desc);

create index if not exists idx_tasks_domain_published
  on tasks (domain_id, published_at desc);

create index if not exists idx_lead_events_utm_form_created
  on lead_events (utm_code, form_id, created_at desc);

create index if not exists idx_intelligence_items_domain_captured
  on intelligence_items (domain_id, captured_at desc);

create index if not exists idx_publishing_records_task_created
  on publishing_records (task_id, created_at desc);

create index if not exists idx_audit_logs_target_created
  on audit_logs (target_type, target_id, created_at desc);

create index if not exists idx_intelligence_embedding_hnsw
  on intelligence_items using hnsw (embedding vector_cosine_ops);

