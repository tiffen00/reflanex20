-- supabase/migrations/002_antibot.sql
-- Anti-bot protection: adds protection_level to links, creates bot_hits table.
-- Run this migration after deploying the anti-bot PR.

-- 1. Add protection_level column to links (idempotent)
alter table public.links
  add column if not exists protection_level text not null default 'standard';

-- 2. Create bot_hits table
create table if not exists public.bot_hits (
  id           bigserial primary key,
  link_id      bigint references public.links(id) on delete cascade,
  slug         text,
  ip           text,
  user_agent   text,
  country      text,
  reason       text not null,
  score        int,
  hit_at       timestamptz not null default now()
);

create index if not exists bot_hits_link_id_idx on public.bot_hits (link_id);
create index if not exists bot_hits_hit_at_idx  on public.bot_hits (hit_at desc);
create index if not exists bot_hits_reason_idx  on public.bot_hits (reason);
