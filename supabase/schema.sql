-- supabase/schema.sql
create table public.campaigns (
  id           bigserial primary key,
  name         text not null,
  version      int  not null default 1,
  is_current   boolean not null default true,
  storage_path text not null,
  entry_file   text not null default '',
  original_filename text,
  is_protected boolean not null default false,
  created_at   timestamptz not null default now(),
  unique (name, version)
);
create index on public.campaigns (name);
create index on public.campaigns (is_current);

create table public.links (
  id           bigserial primary key,
  slug         text not null unique,
  campaign_id  bigint not null references public.campaigns(id) on delete cascade,
  domain       text,
  is_active    boolean not null default true,
  click_limit  int,
  expires_at   timestamptz,
  created_at   timestamptz not null default now()
);
create index on public.links (campaign_id);
create index on public.links (slug);

create table public.clicks (
  id          bigserial primary key,
  link_id     bigint not null references public.links(id) on delete cascade,
  ip          text,
  user_agent  text,
  country     text,
  referer     text,
  clicked_at  timestamptz not null default now()
);
create index on public.clicks (link_id);
create index on public.clicks (clicked_at desc);
create index on public.clicks (country);

create table public.geo_rules (
  id        bigserial primary key,
  link_id   bigint not null references public.links(id) on delete cascade,
  mode      text not null check (mode in ('allow', 'block')),
  countries text[] not null default '{}',
  created_at timestamptz not null default now()
);
create unique index on public.geo_rules (link_id);

create table public.click_alerts (
  id              bigserial primary key,
  link_id         bigint not null references public.links(id) on delete cascade,
  threshold       int not null,
  notified        boolean not null default false,
  created_at      timestamptz not null default now()
);
create index on public.click_alerts (link_id);

create or replace view public.link_stats as
select
  l.id as link_id,
  l.slug,
  l.campaign_id,
  count(c.id) as total_clicks,
  count(distinct c.ip) as unique_visitors,
  max(c.clicked_at) as last_click_at
from public.links l
left join public.clicks c on c.link_id = l.id
group by l.id;
