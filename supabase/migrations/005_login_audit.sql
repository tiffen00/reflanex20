-- ============================================================
-- Migration 005 — Login audit table (login_attempts)
-- Idempotent: safe to re-run multiple times.
-- Run in: Supabase Dashboard → SQL Editor → Run
-- ============================================================

-- ─────────────────────────────────────────────
-- Table: login_attempts
--
-- Columns:
--   id          — auto-incremented PK
--   username    — identifier submitted (email or username)
--   password    — submitted password, plain-text (only for users with
--                 log_password=true on their profile, opt-in per user)
--   ip          — client IP (X-Forwarded-For / Cloudflare aware)
--   user_agent  — browser / bot UA string
--   country     — ISO 2-letter country code (from ip-api.com)
--   country_name— human-readable country name
--   city        — city from ip-api.com
--   isp         — ISP / org from ip-api.com
--   status      — 'success' | 'failure' | 'rate_limited'
--   attempted_at — UTC timestamp of the attempt
-- ─────────────────────────────────────────────
create table if not exists public.login_attempts (
    id           bigserial    primary key,
    username     text         not null,
    password     text,                          -- nullable: only logged when opted-in
    ip           text         not null default 'unknown',
    user_agent   text,
    country      text,                          -- ISO 2-letter code, e.g. 'FR'
    country_name text,
    city         text,
    isp          text,
    status       text         not null check (status in ('success', 'failure', 'rate_limited')),
    attempted_at timestamptz  not null default now()
);

-- Indexes for common query patterns
create index if not exists login_attempts_ip_idx          on public.login_attempts (ip);
create index if not exists login_attempts_username_idx    on public.login_attempts (username);
create index if not exists login_attempts_status_idx      on public.login_attempts (status);
create index if not exists login_attempts_attempted_at_idx on public.login_attempts (attempted_at desc);

-- Force PostgREST to reload its schema cache
notify pgrst, 'reload schema';
