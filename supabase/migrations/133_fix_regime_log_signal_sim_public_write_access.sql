-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 133: Fix public write/truncate access on nt_regime_log + nt_signal_sim
--
-- Security fix (2026-09-04, found while scoping nobletradingapp-backend).
--
-- nt_regime_log had a policy literally named "Service role full access"
-- that was actually scoped `roles={public}` with `USING (true)` — i.e. it
-- did NOT check auth.role() = 'service_role' at all, granting every role
-- (including anon) unrestricted SELECT/INSERT/UPDATE/DELETE. Two OTHER
-- correctly-scoped duplicate policies already existed on the same table
-- (properly checking auth.role() = 'service_role'), so this was a stale/
-- broken leftover that was never dropped — RLS policies are OR'd, so the
-- broken permissive one alone reopened the hole regardless of the correct
-- ones sitting next to it.
--
-- Separately, both nt_regime_log and nt_signal_sim had raw object-level
-- GRANTs (INSERT/UPDATE/DELETE/TRUNCATE) to anon and/or authenticated that
-- were never intended — TRUNCATE in particular is NOT governed by RLS
-- policies at all in Postgres, so that grant alone let anon/authenticated
-- wipe either table regardless of any policy.
--
-- Applied directly to the NobleTrading Supabase project on 2026-09-04
-- (this file exists for history/tracking — verify it's a no-op if re-run).
-- ═══════════════════════════════════════════════════════════════════════════

DROP POLICY IF EXISTS "Service role full access" ON public.nt_regime_log;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON public.nt_regime_log FROM anon, authenticated;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON public.nt_signal_sim FROM anon, authenticated;

-- ═══════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION (paste into Supabase SQL editor):
--   select table_name, grantee, string_agg(privilege_type, ',' order by privilege_type)
--   from information_schema.role_table_grants
--   where table_name in ('nt_regime_log','nt_signal_sim') and grantee in ('anon','authenticated')
--   group by table_name, grantee order by table_name, grantee;
--   Expect: exactly one row per (table, grantee), privs = 'SELECT' only.
-- ═══════════════════════════════════════════════════════════════════════════
