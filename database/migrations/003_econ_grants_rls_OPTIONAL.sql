-- 003_econ_grants_rls_OPTIONAL.sql
-- Belongs to Evergreen ECON, not the bot. Apply SEPARATELY and only after
-- migration 002 is verified.
--
-- econ_grants is the only econ_ table with Row Level Security off, and the
-- public "anon" role can read and write it. The Econ site's .env.example
-- shows it uses SUPABASE_SERVICE_KEY (which bypasses RLS), so enabling RLS
-- should not change how the site works.
--
-- AFTER RUNNING: open the Econ site and test one GRANT request end to end
-- (create, view, approve or reject). If anything fails, roll back:
--   alter table public.econ_grants disable row level security;

alter table public.econ_grants enable row level security;
