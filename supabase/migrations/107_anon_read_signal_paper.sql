    -- ═══════════════════════════════════════════════════════════════════════════
    -- Migration 107: Anonymous read-only access for signal-validation + paper views
    --
    -- WHY: the noble-trader-admin desktop plugin is distributed to MULTIPLE users,
    -- so it CANNOT embed the Supabase service-role key (a secret — never ships in
    -- a plugin). Signals are already shared publicly (proxy signal stream) and the
    -- paper portfolio is the public validation of those signals. This migration
    -- grants the `anon` role SELECT-ONLY access via scoped RLS policies + table
    -- GRANTs. Writes (sweep capture, EOD resolution, paper_book) remain exclusive
    -- to the service-role key held by the backend — the anon role can READ, never
    -- write.
    --
    -- SCOPE (exactly what the plugin fetches):
    --   base tables : nt_signal_sim, nt_paper_positions, nt_sweep_result
    --   views       : v_paper_equity, v_paper_perf, v_paper_exposure,
    --                 v_paper_vs_optimized_daily, v_paper_vs_optimized_symbol,
    --                 v_eod_calibration_bias, v_eod_sweep_drift,
    --                 v_eod_exit_attribution, v_eod_parameter_decay
    --
    -- IDEMPOTENT: DROP POLICY IF EXISTS before CREATE POLICY (CREATE POLICY has no
    -- IF NOT EXISTS → 42710 on re-run). GRANT ... TO anon is naturally idempotent.
    -- ═══════════════════════════════════════════════════════════════════════════

    -- ── 1. RLS read-only policies on base tables ────────────────────────────────
    -- FOR SELECT only — no WITH CHECK clause, so anon can never INSERT/UPDATE/DELETE.
    ALTER TABLE nt_signal_sim ENABLE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS "anon read nt_signal_sim" ON nt_signal_sim;
    CREATE POLICY "anon read nt_signal_sim" ON nt_signal_sim
    FOR SELECT USING (true);

    ALTER TABLE nt_paper_positions ENABLE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS "anon read nt_paper_positions" ON nt_paper_positions;
    CREATE POLICY "anon read nt_paper_positions" ON nt_paper_positions
    FOR SELECT USING (true);

    ALTER TABLE nt_sweep_result ENABLE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS "anon read nt_sweep_result" ON nt_sweep_result;
    CREATE POLICY "anon read nt_sweep_result" ON nt_sweep_result
    FOR SELECT USING (true);

    -- ── 2. GRANT SELECT on base tables to anon (privilege layer) ────────────────
    GRANT SELECT ON nt_signal_sim TO anon;
    GRANT SELECT ON nt_paper_positions TO anon;
    GRANT SELECT ON nt_sweep_result TO anon;

    -- ── 3. GRANT SELECT on the read-only views to anon ──────────────────────────
    GRANT SELECT ON v_paper_equity TO anon;
    GRANT SELECT ON v_paper_perf TO anon;
    GRANT SELECT ON v_paper_exposure TO anon;
    GRANT SELECT ON v_paper_vs_optimized_daily TO anon;
    GRANT SELECT ON v_paper_vs_optimized_symbol TO anon;
    GRANT SELECT ON v_eod_calibration_bias TO anon;
    GRANT SELECT ON v_eod_sweep_drift TO anon;
    GRANT SELECT ON v_eod_exit_attribution TO anon;
    GRANT SELECT ON v_eod_parameter_decay TO anon;

    -- ═══════════════════════════════════════════════════════════════════════════
    -- POST-RUN VERIFICATION (paste into Supabase SQL editor):
    --   SELECT schemaname, tablename, policyname, cmd FROM pg_policies
    --     WHERE tablename IN ('nt_signal_sim','nt_paper_positions','nt_sweep_result')
    --     ORDER BY tablename;
    --   Expect 6 rows (2 per table: "Service role full access" FOR ALL + "anon read" FOR SELECT).
    -- ═══════════════════════════════════════════════════════════════════════════
