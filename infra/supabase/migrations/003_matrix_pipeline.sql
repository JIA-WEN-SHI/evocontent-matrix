-- Matrix pipeline extension (non-breaking)
-- Adds extensible task + metrics + evolution + tool profile tables.

create table if not exists pipeline_tasks (
  id uuid primary key default gen_random_uuid(),
  legacy_task_id uuid references tasks(id) on delete set null,
  domain_id uuid not null references domains(id) on delete cascade,
  channel text not null check (channel in ('xiaohongshu', 'wechat_mp', 'douyin', 'video')),
  content_type text not null default 'post',
  status text not null default 'queued' check (
    status in (
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
    )
  ),
  stage text not null default 'intake',
  intent_jsonb jsonb not null default '{}'::jsonb,
  payload_jsonb jsonb not null default '{}'::jsonb,
  review_jsonb jsonb not null default '{}'::jsonb,
  publish_jsonb jsonb not null default '{}'::jsonb,
  metrics_jsonb jsonb not null default '{}'::jsonb,
  scheduled_at timestamptz,
  published_at timestamptz,
  created_by text not null default 'system',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists task_metrics (
  id uuid primary key default gen_random_uuid(),
  pipeline_task_id uuid not null references pipeline_tasks(id) on delete cascade,
  metric_window text not null default '24h',
  metrics_jsonb jsonb not null default '{}'::jsonb,
  score numeric(8, 4) not null default 0,
  captured_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create table if not exists evolution_insights (
  id uuid primary key default gen_random_uuid(),
  target_type text not null check (target_type in ('domain_strategy', 'functional_tool', 'pipeline_policy')),
  target_id text not null,
  domain_id uuid references domains(id) on delete set null,
  pipeline_task_id uuid references pipeline_tasks(id) on delete set null,
  insight_jsonb jsonb not null default '{}'::jsonb,
  action_jsonb jsonb not null default '{}'::jsonb,
  confidence numeric(5, 4) not null default 0,
  applied boolean not null default false,
  applied_at timestamptz,
  created_by text not null default 'system:analyst',
  created_at timestamptz not null default now()
);

create table if not exists tool_profiles (
  id uuid primary key default gen_random_uuid(),
  tool_key text not null unique,
  name text not null,
  enabled boolean not null default true,
  config_jsonb jsonb not null default '{}'::jsonb,
  perf_jsonb jsonb not null default '{}'::jsonb,
  last_reflection_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_pipeline_tasks_domain_status_created
  on pipeline_tasks (domain_id, status, created_at desc);

create index if not exists idx_pipeline_tasks_channel_status_created
  on pipeline_tasks (channel, status, created_at desc);

create index if not exists idx_task_metrics_task_captured
  on task_metrics (pipeline_task_id, captured_at desc);

create index if not exists idx_evolution_insights_target_created
  on evolution_insights (target_type, target_id, created_at desc);

create index if not exists idx_tool_profiles_enabled_updated
  on tool_profiles (enabled, updated_at desc);

insert into tool_profiles (tool_key, name, enabled, config_jsonb)
values
  (
    'copywriter.default',
    'Default Copywriter',
    true,
    jsonb_build_object('provider', 'router', 'mode', 'multi', 'version', 'v1')
  ),
  (
    'image_brief.default',
    'Default Image Brief Generator',
    true,
    jsonb_build_object('style', 'trustworthy_japan', 'version', 'v1')
  ),
  (
    'publisher.playwright',
    'Playwright Publisher',
    true,
    jsonb_build_object('headless', true, 'retry_policy', 'default')
  )
on conflict (tool_key) do nothing;
