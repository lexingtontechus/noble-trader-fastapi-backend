-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 134: Fix nt_paper_positions grants + schema-wide default privileges
--
-- Security fix (2026-09-04, part 2 of the audit started in migration 133).
--
-- Root cause found: this project has `ALTER DEFAULT PRIVILEGES IN SCHEMA
-- public` granting full table privileges (INSERT/SELECT/UPDATE/DELETE/
-- TRUNCATE/REFERENCES/TRIGGER) to `anon` AND `authenticated` on every NEW
-- table, silently, with no GRANT statement ever needing to run. This is how
-- nt_regime_log (migration 133) ended up with those grants despite a
-- correctly-scoped RLS policy sitting right next to a broken one, and how
-- nobletradingapp's own nt_correlation_cache (migration 0011/0014) ended up
-- fully public despite no one explicitly granting anon anything.
--
-- Confirmed also affecting nt_paper_positions (pre-existing, LIVE
-- paying-subscriber data): its RLS policies are correctly scoped to
-- service_role for writes, but TRUNCATE is not governed by RLS at all in
-- Postgres — the raw TRUNCATE grant alone let any anon/authenticated caller
-- (i.e. any signed-in subscriber, via the browser-exposed anon key) wipe the
-- entire paper-positions table for every user on the platform.
--
-- This migration: (1) trims nt_paper_positions' grants to SELECT only for
-- anon/authenticated, matching its actual intended access (its own RLS
-- policies already correctly gate everything else), and (2) revokes the
-- schema-wide default so this stops recurring on every future table.
--
-- NOT a full retroactive audit of every table in the schema — that's a
-- separate, larger pass. Tables known-fixed so far: nt_regime_log (133),
-- nt_signal_sim (133), nt_paper_positions + nt_correlation_cache (this
-- migration + nobletradingapp migration 0014).
-- ═══════════════════════════════════════════════════════════════════════════

REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON public.nt_paper_positions FROM anon, authenticated;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE ALL ON TABLES FROM anon, authenticated;

-- ═══════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION (paste into Supabase SQL editor):
--   select table_name, grantee, string_agg(privilege_type, ',' order by privilege_type)
--   from information_schema.role_table_grants
--   where table_name = 'nt_paper_positions' and grantee in ('anon','authenticated')
--   group by table_name, grantee;
--   Expect: exactly one row per grantee, privs = 'SELECT' only.
--
--   To confirm the schema default is fixed, create any throwaway table and
--   check it has ZERO rows for anon/authenticated in role_table_grants
--   before any explicit GRANT is run against it.
--
-- STILL OUTSTANDING: a full audit of every other table in `public` for the
-- same pre-existing-grant issue (this migration only fixes the schema
-- default for FUTURE tables + the two tables confirmed affected so far).
-- ═══════════════════════════════════════════════════════════════════════════
