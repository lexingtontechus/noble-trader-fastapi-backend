-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 222: Retroactive write-grant audit on nt_* tables + DEFINER revoke
--
-- Security fix (2026-11-18, hermes-security scan scan_d5e17dd9d64a3cf9d2d49860,
-- findings cand_6c1d587169abdc23dd072d2f / cand_aefce02902f6d4ed21486979).
-- Closes the "STILL OUTSTANDING" retroactive audit deferred by migration 134.
--
-- Live-verified before writing (information_schema.role_table_grants):
--   * 17 nt_* tables still grant INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/
--     TRIGGER to BOTH anon and authenticated (pre-134 default-privilege
--     residue), incl. nt_system_config (kill-switch state), nt_calibration_
--     records, nt_eod_actions, nt_tuning_params, nt_macro_regime, ...
--   * nt_symbol / nt_sweep_result / nt_renko_bricks: migration 112 revoked
--     only INSERT/UPDATE/DELETE from anon — REFERENCES/TRIGGER/TRUNCATE
--     survived, and authenticated kept full ALL. TRUNCATE bypasses RLS
--     entirely, so the SELECT-only policies do not stop a table wipe.
--   * refresh_talaria_dashboard_cache() (SECURITY DEFINER, migration 193)
--     carries anon=X / authenticated=X in its ACL — any anon caller can
--     force a matview refresh + cache upsert under the definer's rights.
--
-- Fix (privilege layer only — no RLS/policy changes, no new grants):
--   1. REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER on every
--      public nt_% table FROM anon, authenticated. Existing SELECT grants
--      are preserved exactly as-is (Talaria/plugins/dashboard read path).
--   2. REVOKE EXECUTE on refresh_talaria_dashboard_cache() from PUBLIC,
--      anon, authenticated; grant only postgres + service_role (pg_cron
--      job 924 runs as postgres).
--
-- Scope guarantee: the DO block touches ONLY tables named nt_%. The
-- nobletradingapp commerce tables (users/subscriptions/plans/...) share this
-- project but are NOT nt_% and are untouched. All backend writes use the
-- service key (repo guide §5), so the sweep/orchestrator/EOD paths are
-- unaffected. Idempotent: REVOKE of an absent grant is a no-op.
-- ═══════════════════════════════════════════════════════════════════════════

DO $$
DECLARE
  t text;
BEGIN
  FOR t IN
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public' AND tablename LIKE 'nt\_%'
  LOOP
    EXECUTE format(
      'REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON public.%I FROM anon, authenticated',
      t
    );
  END LOOP;
END $$;

REVOKE EXECUTE ON FUNCTION public.refresh_talaria_dashboard_cache()
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.refresh_talaria_dashboard_cache()
  TO postgres, service_role;

-- ═══════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION:
--   SELECT table_name, grantee, string_agg(privilege_type, ',' ORDER BY privilege_type)
--   FROM information_schema.role_table_grants
--   WHERE table_schema='public' AND table_name LIKE 'nt\_%'
--     AND grantee IN ('anon','authenticated')
--   GROUP BY table_name, grantee;
--   Expect: privs = 'SELECT' only (where a SELECT grant existed before).
--
--   SELECT proacl FROM pg_proc WHERE proname='refresh_talaria_dashboard_cache';
--   Expect: postgres=X + service_role=X only (no anon/authenticated/PUBLIC).
--
--   Backend smoke: next light sweep writes nt_sweep_result normally (service
--   key); Talaria/dashboard reads still return rows; pg_cron job 924 refresh
--   still succeeds.
-- ═══════════════════════════════════════════════════════════════════════════
