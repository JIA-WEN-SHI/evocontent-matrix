-- Knowledge base core entities
-- Adds cases/assets/user_needs/topics/reviews with shared audit fields.

create extension if not exists pgcrypto;

create or replace function kb_touch_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create table if not exists cases (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  platform text not null default 'xiaohongshu',
  author text not null default '',
  url text not null default '',
  title text not null,
  content text not null default '',
  metrics jsonb not null default '{}'::jsonb,
  hook text not null default '',
  structure text not null default '',
  analysis text not null default '',
  source_type text not null default 'manual',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists assets (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  type text not null default 'insight',
  content text not null,
  source text not null default '',
  usable_scene text not null default '',
  is_verified boolean not null default false,
  summary text not null default '',
  source_type text not null default 'manual',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists user_needs (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  user_type text not null default '',
  original_text text not null,
  scenario text not null default '',
  demand_type text not null default '',
  emotion text not null default '',
  real_problem text not null default '',
  source_type text not null default 'manual',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists topics (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  title text not null,
  topic_description text not null default '',
  target_user text not null default '',
  platform text not null default 'xiaohongshu',
  structure_type text not null default '',
  status text not null default 'todo' check (status in ('todo', 'drafted', 'produced', 'published', 'archived')),
  reason text not null default '',
  source_type text not null default 'manual',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists reviews (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  topic_id uuid references topics(id) on delete set null,
  pipeline_task_id uuid references pipeline_tasks(id) on delete set null,
  content_item_ref text not null default '',
  platform text not null default 'xiaohongshu',
  metrics jsonb not null default '{}'::jsonb,
  success_points text not null default '',
  failure_points text not null default '',
  improvement text not null default '',
  summary_text text not null default '',
  source_type text not null default 'manual',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists idx_cases_domain_platform_url_active
  on cases (domain_id, platform, url)
  where deleted_at is null and url <> '';

create index if not exists idx_cases_domain_created
  on cases (domain_id, created_at desc);

create index if not exists idx_assets_domain_type_updated
  on assets (domain_id, type, updated_at desc)
  where deleted_at is null;

create index if not exists idx_user_needs_domain_demand_emotion
  on user_needs (domain_id, demand_type, emotion, created_at desc)
  where deleted_at is null;

create index if not exists idx_topics_domain_status_created
  on topics (domain_id, status, created_at desc)
  where deleted_at is null;

create index if not exists idx_reviews_domain_platform_created
  on reviews (domain_id, platform, created_at desc)
  where deleted_at is null;

do $$
begin
  if not exists (select 1 from pg_trigger where tgname = 'trg_cases_touch_updated_at') then
    create trigger trg_cases_touch_updated_at before update on cases
      for each row execute function kb_touch_updated_at();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'trg_assets_touch_updated_at') then
    create trigger trg_assets_touch_updated_at before update on assets
      for each row execute function kb_touch_updated_at();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'trg_user_needs_touch_updated_at') then
    create trigger trg_user_needs_touch_updated_at before update on user_needs
      for each row execute function kb_touch_updated_at();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'trg_topics_touch_updated_at') then
    create trigger trg_topics_touch_updated_at before update on topics
      for each row execute function kb_touch_updated_at();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'trg_reviews_touch_updated_at') then
    create trigger trg_reviews_touch_updated_at before update on reviews
      for each row execute function kb_touch_updated_at();
  end if;
end $$;
