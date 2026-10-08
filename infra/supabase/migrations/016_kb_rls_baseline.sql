-- RLS baseline for KB tables.
-- Default deny anon, allow service_role for API-side operations.

do $$
declare
  tbl text;
  tables text[] := array[
    'cases',
    'assets',
    'user_needs',
    'topics',
    'reviews',
    'tags',
    'entity_tags',
    'topic_case_links',
    'topic_asset_links',
    'topic_need_links',
    'ingestion_logs',
    'ai_jobs'
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
