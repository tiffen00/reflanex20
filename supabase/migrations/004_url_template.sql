-- Migration 004: add url_template column to links table
-- This stores the full URL path template (e.g. "secure/account/verify/<slug>/auth")
-- for each link so the long URL can be reconstructed exactly on display.
-- Legacy links (8-char slugs) will have url_template = NULL and continue to
-- use the /c/<slug>/ route.

alter table public.links
    add column if not exists url_template text;
