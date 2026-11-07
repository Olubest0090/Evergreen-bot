-- 002_enable_rls_and_delete_at.sql
-- Evergreen Bot -- Phase 1 database hardening (additive, reversible).
--
-- 1. Turns on Row Level Security for every bot table. The bot and the
--    Evergreen Econ site both use the Supabase SERVICE key, which bypasses
--    RLS, so neither is affected. The public "anon" key loses all access.
-- 2. Adds war_rooms.delete_at, which the code already tries to write
--    (schedule_war_room_delete) but which did not exist.
--
-- Does NOT touch any econ_ table. Does NOT delete or change any row.
-- Run the whole script at once in the Supabase SQL Editor.

begin;

alter table public.ai_knowledge          enable row level security;
alter table public.alerts_config         enable row level security;
alter table public.bot_flags             enable row level security;
alter table public.coalitions            enable row level security;
alter table public.dnr_settings          enable row level security;
alter table public.guild_roles           enable row level security;
alter table public.nation_links          enable row level security;
alter table public.nation_spy_tracking   enable row level security;
alter table public.seen_attacks          enable row level security;
alter table public.seen_wars             enable row level security;
alter table public.sphere_alliances      enable row level security;
alter table public.sphere_config         enable row level security;
alter table public.sphere_spy_tracking   enable row level security;
alter table public.sphere_wars           enable row level security;
alter table public.war_room_participants enable row level security;
alter table public.war_rooms             enable row level security;
alter table public.warroom_brackets      enable row level security;
alter table public.warroom_settings      enable row level security;

alter table public.war_rooms add column if not exists delete_at timestamptz;

commit;

-- VERIFY (run separately afterwards):
--   select relname, relrowsecurity from pg_class
--   where relnamespace = 'public'::regnamespace and relkind = 'r'
--     and relname not like 'econ\_%' order by relname;
--   -> all 18 rows must show relrowsecurity = true
--
--   select column_name from information_schema.columns
--   where table_schema='public' and table_name='war_rooms' and column_name='delete_at';
--   -> one row
--
-- ROLLBACK (only if something unexpected happens):
--   alter table public.<table> disable row level security;   -- per table
--   alter table public.war_rooms drop column delete_at;
