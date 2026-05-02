-- supabase/migrations/003_protected_campaigns.sql
-- Add is_protected column to campaigns table
alter table public.campaigns
  add column if not exists is_protected boolean not null default false;
