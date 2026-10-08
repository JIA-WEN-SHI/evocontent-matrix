-- Channel account management (Xiaohongshu/Douyin/etc.)
-- Stores per-account login state paths and publishing preferences.

create table if not exists channel_accounts (
  id uuid primary key default gen_random_uuid(),
  channel text not null check (channel in ('xiaohongshu', 'wechat_mp', 'douyin', 'video')),
  account_name text not null,
  account_handle text,
  login_mode text not null default 'storage_state' check (login_mode in ('storage_state', 'user_data_dir', 'cookies_json', 'credential')),
  storage_state_path text,
  user_data_dir text,
  cookies_json text,
  login_username text,
  login_password text,
  publish_selector text not null default '',
  is_active boolean not null default true,
  tags text[] not null default '{}'::text[],
  config_jsonb jsonb not null default '{}'::jsonb,
  notes text,
  last_login_check_at timestamptz,
  last_login_check_status text,
  last_login_check_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_channel_accounts_channel_active_updated
  on channel_accounts (channel, is_active, updated_at desc);

create index if not exists idx_channel_accounts_name
  on channel_accounts (account_name);
