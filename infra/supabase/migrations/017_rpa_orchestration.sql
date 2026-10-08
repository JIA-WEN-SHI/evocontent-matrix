-- RPA orchestration tables for LLM->Octopus->KB closed loop.
-- Includes template registry, run ledger, raw callback payloads, and clean/accept results.

create extension if not exists pgcrypto;

create table if not exists rpa_task_templates (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  task_type text not null,
  entity_type text not null check (entity_type in ('case', 'asset', 'user_need', 'review')),
  octopus_flow_id text not null,
  octopus_endpoint text not null default '/openapi/flows/run',
  instruction_schema jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists rpa_task_runs (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  instruction_id text not null,
  task_type text not null,
  entity_type text not null check (entity_type in ('case', 'asset', 'user_need', 'review')),
  source_run_id text not null default '',
  status text not null default 'queued' check (
    status in (
      'queued',
      'dispatching',
      'dispatched',
      'callback_received',
      'cleaning',
      'accepted',
      'partial_failed',
      'rejected',
      'failed',
      'canceled'
    )
  ),
  octopus_run_id text not null default '',
  octopus_request jsonb not null default '{}'::jsonb,
  octopus_response jsonb not null default '{}'::jsonb,
  callback_payload jsonb not null default '{}'::jsonb,
  accepted_count integer not null default 0,
  rejected_count integer not null default 0,
  duplicate_count integer not null default 0,
  retry_count integer not null default 0,
  error_message text,
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists rpa_raw_payloads (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  run_id uuid not null references rpa_task_runs(id) on delete cascade,
  source_run_id text not null default '',
  entity_type text not null check (entity_type in ('case', 'asset', 'user_need', 'review')),
  raw_ref text not null default '',
  raw_item jsonb not null default '{}'::jsonb,
  status text not null default 'received' check (status in ('received', 'normalized', 'invalid')),
  reject_code text not null default '',
  reject_reason text not null default '',
  created_at timestamptz not null default now()
);

create table if not exists rpa_clean_results (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  run_id uuid not null references rpa_task_runs(id) on delete cascade,
  raw_payload_id uuid not null references rpa_raw_payloads(id) on delete cascade,
  entity_type text not null check (entity_type in ('case', 'asset', 'user_need', 'review')),
  normalized_item jsonb not null default '{}'::jsonb,
  accepted boolean not null default false,
  duplicate boolean not null default false,
  inserted_entity_id uuid,
  reject_code text not null default '',
  reject_reason text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists idx_rpa_task_templates_domain_task_active
  on rpa_task_templates (domain_id, lower(task_type))
  where deleted_at is null;

create index if not exists idx_rpa_task_templates_domain_entity
  on rpa_task_templates (domain_id, entity_type, is_active, updated_at desc)
  where deleted_at is null;

create unique index if not exists idx_rpa_task_runs_domain_instruction_active
  on rpa_task_runs (domain_id, instruction_id)
  where deleted_at is null;

create index if not exists idx_rpa_task_runs_domain_status_updated
  on rpa_task_runs (domain_id, status, updated_at desc)
  where deleted_at is null;

create index if not exists idx_rpa_task_runs_octopus_run_id
  on rpa_task_runs (octopus_run_id, updated_at desc)
  where deleted_at is null and octopus_run_id <> '';

create index if not exists idx_rpa_raw_payloads_run_status
  on rpa_raw_payloads (run_id, status, created_at desc);

create index if not exists idx_rpa_raw_payloads_domain_entity
  on rpa_raw_payloads (domain_id, entity_type, created_at desc);

create unique index if not exists idx_rpa_clean_results_raw_payload
  on rpa_clean_results (raw_payload_id);

create index if not exists idx_rpa_clean_results_run_accepted
  on rpa_clean_results (run_id, accepted, created_at desc);

create index if not exists idx_rpa_clean_results_reject_code
  on rpa_clean_results (reject_code, created_at desc)
  where accepted = false;

do $$
begin
  if not exists (select 1 from pg_trigger where tgname = 'trg_rpa_task_templates_touch_updated_at') then
    create trigger trg_rpa_task_templates_touch_updated_at before update on rpa_task_templates
      for each row execute function kb_touch_updated_at();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'trg_rpa_task_runs_touch_updated_at') then
    create trigger trg_rpa_task_runs_touch_updated_at before update on rpa_task_runs
      for each row execute function kb_touch_updated_at();
  end if;
  if not exists (select 1 from pg_trigger where tgname = 'trg_rpa_clean_results_touch_updated_at') then
    create trigger trg_rpa_clean_results_touch_updated_at before update on rpa_clean_results
      for each row execute function kb_touch_updated_at();
  end if;
end $$;

do $$
declare
  d_id uuid;
begin
  select id into d_id from domains where slug = 'japan_immigration' limit 1;
  if d_id is not null then
    if not exists (
      select 1 from rpa_task_templates
      where domain_id = d_id and lower(task_type) = 'collect_case' and deleted_at is null
    ) then
      insert into rpa_task_templates (domain_id, task_type, entity_type, octopus_flow_id, created_by)
      values (d_id, 'collect_case', 'case', 'octopus_collect_case', 'system:seed');
    end if;
    if not exists (
      select 1 from rpa_task_templates
      where domain_id = d_id and lower(task_type) = 'collect_asset' and deleted_at is null
    ) then
      insert into rpa_task_templates (domain_id, task_type, entity_type, octopus_flow_id, created_by)
      values (d_id, 'collect_asset', 'asset', 'octopus_collect_asset', 'system:seed');
    end if;
    if not exists (
      select 1 from rpa_task_templates
      where domain_id = d_id and lower(task_type) = 'collect_user_need' and deleted_at is null
    ) then
      insert into rpa_task_templates (domain_id, task_type, entity_type, octopus_flow_id, created_by)
      values (d_id, 'collect_user_need', 'user_need', 'octopus_collect_user_need', 'system:seed');
    end if;
    if not exists (
      select 1 from rpa_task_templates
      where domain_id = d_id and lower(task_type) = 'collect_review' and deleted_at is null
    ) then
      insert into rpa_task_templates (domain_id, task_type, entity_type, octopus_flow_id, created_by)
      values (d_id, 'collect_review', 'review', 'octopus_collect_review', 'system:seed');
    end if;
  end if;
end $$;

do $$
declare
  tbl text;
  tables text[] := array[
    'rpa_task_templates',
    'rpa_task_runs',
    'rpa_raw_payloads',
    'rpa_clean_results'
  ];
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
