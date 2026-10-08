-- Knowledge base tags and link tables

create extension if not exists pgcrypto;

create table if not exists tags (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  name text not null,
  category text not null default 'general',
  status text not null default 'active' check (status in ('active', 'disabled')),
  source_type text not null default 'manual',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists entity_tags (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  entity_type text not null check (entity_type in ('case', 'asset', 'user_need', 'topic', 'review')),
  entity_id uuid not null,
  tag_id uuid not null references tags(id) on delete cascade,
  source_type text not null default 'manual',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists topic_case_links (
  id uuid primary key default gen_random_uuid(),
  topic_id uuid not null references topics(id) on delete cascade,
  case_id uuid not null references cases(id) on delete cascade,
  created_by text not null default 'system',
  created_at timestamptz not null default now()
);

create table if not exists topic_asset_links (
  id uuid primary key default gen_random_uuid(),
  topic_id uuid not null references topics(id) on delete cascade,
  asset_id uuid not null references assets(id) on delete cascade,
  created_by text not null default 'system',
  created_at timestamptz not null default now()
);

create table if not exists topic_need_links (
  id uuid primary key default gen_random_uuid(),
  topic_id uuid not null references topics(id) on delete cascade,
  user_need_id uuid not null references user_needs(id) on delete cascade,
  created_by text not null default 'system',
  created_at timestamptz not null default now()
);

create unique index if not exists idx_tags_domain_name_category_active
  on tags (domain_id, lower(name), lower(category))
  where deleted_at is null;

create index if not exists idx_tags_domain_category_status
  on tags (domain_id, category, status, updated_at desc)
  where deleted_at is null;

create unique index if not exists idx_entity_tags_unique_active
  on entity_tags (entity_type, entity_id, tag_id)
  where deleted_at is null;

create index if not exists idx_entity_tags_domain_entity
  on entity_tags (domain_id, entity_type, entity_id, created_at desc)
  where deleted_at is null;

create unique index if not exists idx_topic_case_links_unique
  on topic_case_links (topic_id, case_id);

create unique index if not exists idx_topic_asset_links_unique
  on topic_asset_links (topic_id, asset_id);

create unique index if not exists idx_topic_need_links_unique
  on topic_need_links (topic_id, user_need_id);

do $$
begin
  if not exists (select 1 from pg_trigger where tgname = 'trg_tags_touch_updated_at') then
    create trigger trg_tags_touch_updated_at before update on tags
      for each row execute function kb_touch_updated_at();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'trg_entity_tags_touch_updated_at') then
    create trigger trg_entity_tags_touch_updated_at before update on entity_tags
      for each row execute function kb_touch_updated_at();
  end if;
end $$;
