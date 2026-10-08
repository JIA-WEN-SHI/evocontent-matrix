-- Intelligence items account scope support
-- Enable per-account crawl isolation for the same domain.

do $$
begin
  alter table intelligence_items
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
      and table_name = 'intelligence_items'
  ) and exists (
    select 1
    from information_schema.tables
    where table_schema = 'public'
      and table_name = 'channel_accounts'
  ) then
    begin
      alter table intelligence_items
        add constraint fk_intelligence_items_account
        foreign key (account_id) references channel_accounts(id) on delete set null;
    exception
      when duplicate_object then
        null;
    end;
  end if;
end$$;

create index if not exists idx_intelligence_items_domain_account_captured
  on intelligence_items (domain_id, account_id, captured_at desc);

create index if not exists idx_intelligence_items_account_source_url
  on intelligence_items (account_id, source_url);
