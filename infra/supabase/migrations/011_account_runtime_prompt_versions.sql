-- Account-aware execution/runtime formalization
-- Adds first-class account_id columns and prompt version history tables.

do $$
begin
  alter table pipeline_tasks
    add column if not exists account_id uuid;
exception
  when undefined_table then
    null;
end$$;

do $$
begin
  if exists (
    select 1
    from information_schema.tables
    where table_schema = 'public'
      and table_name = 'pipeline_tasks'
  ) and exists (
    select 1
    from information_schema.tables
    where table_schema = 'public'
      and table_name = 'channel_accounts'
  ) then
    begin
      alter table pipeline_tasks
        add constraint fk_pipeline_tasks_account
        foreign key (account_id) references channel_accounts(id) on delete set null;
    exception
      when duplicate_object then
        null;
    end;
  end if;
end$$;

update pipeline_tasks
set account_id = nullif(payload_jsonb ->> 'channel_account_id', '')::uuid
where account_id is null
  and coalesce(payload_jsonb ->> 'channel_account_id', '') <> '';

create index if not exists idx_pipeline_tasks_account_status_created
  on pipeline_tasks (account_id, status, created_at desc);

do $$
begin
  alter table evolution_insights
    add column if not exists account_id uuid;
exception
  when undefined_table then
    null;
end$$;

do $$
begin
  if exists (
    select 1
    from information_schema.tables
    where table_schema = 'public'
      and table_name = 'evolution_insights'
  ) and exists (
    select 1
    from information_schema.tables
    where table_schema = 'public'
      and table_name = 'channel_accounts'
  ) then
    begin
      alter table evolution_insights
        add constraint fk_evolution_insights_account
        foreign key (account_id) references channel_accounts(id) on delete set null;
    exception
      when duplicate_object then
        null;
    end;
  end if;
end$$;

update evolution_insights ei
set account_id = pt.account_id
from pipeline_tasks pt
where ei.account_id is null
  and ei.pipeline_task_id = pt.id
  and pt.account_id is not null;

create index if not exists idx_evolution_insights_account_created
  on evolution_insights (account_id, created_at desc);

create table if not exists prompt_versions (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete cascade,
  agent_name text not null,
  version text not null,
  system_prompt text not null,
  status text not null default 'draft' check (status in ('draft', 'active', 'rolled_back', 'archived')),
  source text not null default 'manual' check (source in ('manual', 'reflection', 'chief_evolution', 'system_seed')),
  reason text,
  rolled_back_from uuid references prompt_versions(id) on delete set null,
  created_by text not null default 'system',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists idx_prompt_versions_account_agent_version
  on prompt_versions (account_id, agent_name, version);

create index if not exists idx_prompt_versions_account_agent_status_created
  on prompt_versions (account_id, agent_name, status, created_at desc);

create table if not exists prompt_change_logs (
  id uuid primary key default gen_random_uuid(),
  prompt_version_id uuid not null references prompt_versions(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  change_summary text not null default '',
  evidence_jsonb jsonb not null default '{}'::jsonb,
  approved_by text,
  created_at timestamptz not null default now()
);

create index if not exists idx_prompt_change_logs_prompt_created
  on prompt_change_logs (prompt_version_id, created_at desc);

create index if not exists idx_prompt_change_logs_account_created
  on prompt_change_logs (account_id, created_at desc);
