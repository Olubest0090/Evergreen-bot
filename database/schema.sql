-- =====================================================================
-- Evergreen Bot -- database schema (bot tables only)
-- =====================================================================
-- Reflects the LIVE Supabase database as inspected on 2026-11-07, PLUS the
-- changes made by migration 002 (RLS + war_rooms.delete_at).
--
-- * Safe to run on the live database: every statement is IF NOT EXISTS and
--   changes nothing that already exists. Its real purpose is to build a blank
--   test database for development.
-- * Does NOT include the econ_ tables. Those belong to the evergreen-econ
--   repo (same Supabase project). Never alter them from this repo.
-- * Existing migrations: 001_federal_sphere.sql (sphere tables),
--   002_enable_rls_and_delete_at.sql.
-- * Every table is keyed by guild_id (Discord server) except nation_links
--   and war_room_participants -- see docs/DATABASE.md.
-- =====================================================================

-- ---- Government roles (MA / EA / FA / IA) ---------------------------
create table if not exists public.guild_roles (
  guild_id  bigint not null,
  role_type text   not null,            -- 'MA' | 'EA' | 'FA' | 'IA'
  role_id   bigint not null,            -- Discord role id
  primary key (guild_id, role_type)
);

-- ---- Per-server alert + alliance settings ---------------------------
create table if not exists public.alerts_config (
  guild_id                 bigint  primary key,
  alliance_id              bigint,      -- the P&W alliance this server manages
  defense_channel_id       bigint,
  offensive_channel_id     bigint,
  espionage_channel_id     bigint,
  include_allied_alliances boolean not null default false,
  auto_sync_treaties       boolean not null default false,
  ma_notify_channel_id     bigint,
  auto_counter_enabled     boolean not null default true
);

-- ---- Discord user <-> P&W nation ------------------------------------
create table if not exists public.nation_links (
  discord_user_id bigint primary key,   -- ONE link per Discord user, all servers
  nation_id       bigint not null,      -- not unique: see docs/DATABASE.md
  guild_id        bigint not null       -- server where the link was last made
);

-- ---- "Already alerted" memory for the war alert loop -----------------
create table if not exists public.seen_wars (
  guild_id          bigint not null,
  war_id            bigint not null,
  alerted_defense   boolean default false,
  alerted_offensive boolean default false,
  primary key (guild_id, war_id)
);

-- ---- Last known spy count per nation (espionage detection) ----------
create table if not exists public.nation_spy_tracking (
  guild_id         bigint not null,
  nation_id        bigint not null,
  last_known_spies integer,
  primary key (guild_id, nation_id)
);

-- ---- Do-Not-Raid protection of the top X alliances -------------------
create table if not exists public.dnr_settings (
  guild_id bigint  primary key,
  top_x    integer not null default 0
);

-- ---- Coalition lists (allies, DNR, extension) ------------------------
create table if not exists public.coalitions (
  guild_id      bigint  not null,
  alliance_id   bigint  not null,
  alliance_name text,
  bloc_type     text    not null,       -- 'ALLIES' | 'DNR' | 'EXTENSION'
  auto_synced   boolean not null default false,  -- true = added by treaty sync
  primary key (guild_id, alliance_id, bloc_type)
);

-- ---- One-time command markers ---------------------------------------
create table if not exists public.bot_flags (
  guild_id  bigint not null,
  flag_name text   not null,
  primary key (guild_id, flag_name)
);

-- ---- War rooms -------------------------------------------------------
create table if not exists public.warroom_settings (
  guild_id            bigint  primary key,
  enabled             boolean not null default false,
  war_category_prefix text    not null default 'WARCAT'
);

create table if not exists public.warroom_brackets (   -- city-count ranges
  guild_id   bigint  not null,
  min_cities integer not null,
  max_cities integer not null,
  label      text    not null,          -- category name, e.g. 'c1-20'
  primary key (guild_id, min_cities, max_cities)
);

create table if not exists public.war_rooms (
  guild_id          bigint not null,
  enemy_nation_id   bigint not null,
  channel_id        bigint not null,
  category_id       bigint not null,
  pin_message_id    bigint,
  missing_count     integer default 0,  -- loop cycles the enemy was absent
  objective         text,
  last_orders       text,
  order_assignments jsonb default '{}'::jsonb,
  delete_at         timestamptz,        -- added by migration 002
  primary key (guild_id, enemy_nation_id)
);

create table if not exists public.war_room_participants (
  channel_id      bigint not null,
  discord_user_id bigint not null,
  primary key (channel_id, discord_user_id)
);

create table if not exists public.seen_attacks (       -- attacks already posted
  guild_id  bigint not null,
  attack_id bigint not null,
  primary key (guild_id, attack_id)
);

-- ---- AI knowledge base -----------------------------------------------
create table if not exists public.ai_knowledge (
  id         bigserial primary key,
  guild_id   bigint not null,
  source     text,
  content    text not null,
  url        text,
  created_at timestamptz default now()
);
create index if not exists ai_knowledge_guild_created
  on public.ai_knowledge (guild_id, created_at desc);

-- ---- Federal Sphere (migration 001) ----------------------------------
create table if not exists public.sphere_config (
  guild_id             bigint primary key,
  offensive_channel_id bigint,
  defensive_channel_id bigint,
  spy_channel_id       bigint,
  updated_at           timestamptz not null default now()
);

create table if not exists public.sphere_alliances (
  id            bigint generated by default as identity primary key,
  guild_id      bigint  not null,
  alliance_id   bigint  not null,
  alliance_name text    not null,
  score         numeric,
  active        boolean not null default true,
  added_at      timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  constraint sphere_alliances_guild_alliance_key unique (guild_id, alliance_id)
);
create index if not exists sphere_alliances_active_idx
  on public.sphere_alliances (guild_id, active);

create table if not exists public.sphere_spy_tracking (
  id               bigint generated by default as identity primary key,
  guild_id         bigint  not null,
  alliance_id      bigint  not null,
  nation_id        bigint  not null,
  nation_name      text    not null,
  last_known_spies integer not null default 0,
  last_checked_at  timestamptz not null default now(),
  constraint sphere_spy_tracking_guild_nation_key unique (guild_id, nation_id)
);
create index if not exists sphere_spy_tracking_alliance_idx
  on public.sphere_spy_tracking (guild_id, alliance_id);

create table if not exists public.sphere_wars (
  id                   bigint generated by default as identity primary key,
  guild_id             bigint  not null,
  alliance_id          bigint  not null,
  alliance_name        text    not null,
  war_id               bigint  not null,
  war_type             text,
  turns_left           integer,
  attacker_nation_id   bigint,
  attacker_name        text,
  attacker_alliance_id bigint,
  defender_nation_id   bigint,
  defender_name        text,
  defender_alliance_id bigint,
  offensive_alerted    boolean not null default false,
  defensive_alerted    boolean not null default false,
  first_seen_at        timestamptz not null default now(),
  last_seen_at         timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  constraint sphere_wars_guild_alliance_war_key unique (guild_id, alliance_id, war_id)
);
create index if not exists sphere_wars_alliance_seen_idx
  on public.sphere_wars (guild_id, alliance_id, last_seen_at desc);
create index if not exists sphere_wars_guild_war_idx
  on public.sphere_wars (guild_id, war_id);

-- ---- Row Level Security (migration 002) ------------------------------
-- No policies on purpose: only the service key (used by the bot and the
-- Econ site) may read or write.
alter table public.guild_roles           enable row level security;
alter table public.alerts_config         enable row level security;
alter table public.nation_links          enable row level security;
alter table public.seen_wars             enable row level security;
alter table public.nation_spy_tracking   enable row level security;
alter table public.dnr_settings          enable row level security;
alter table public.coalitions            enable row level security;
alter table public.bot_flags             enable row level security;
alter table public.warroom_settings      enable row level security;
alter table public.warroom_brackets      enable row level security;
alter table public.war_rooms             enable row level security;
alter table public.war_room_participants enable row level security;
alter table public.seen_attacks          enable row level security;
alter table public.ai_knowledge          enable row level security;
alter table public.sphere_config         enable row level security;
alter table public.sphere_alliances      enable row level security;
alter table public.sphere_spy_tracking   enable row level security;
alter table public.sphere_wars           enable row level security;
