-- Add credential login mode for channel accounts.
-- This migration is safe to run multiple times.

alter table if exists channel_accounts
  add column if not exists login_username text;

alter table if exists channel_accounts
  add column if not exists login_password text;

alter table if exists channel_accounts
  drop constraint if exists channel_accounts_login_mode_check;

alter table if exists channel_accounts
  add constraint channel_accounts_login_mode_check
  check (login_mode in ('storage_state', 'user_data_dir', 'cookies_json', 'credential'));
