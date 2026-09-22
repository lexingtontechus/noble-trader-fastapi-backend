-- Migration 163: live expectancy table for p_win_bracket flip evidence (P2)
--
-- Sequence (worklog 20260922_R2_ADDENDUM): hold both modes at shadow until
-- live expectancy shows expired%↓ AND expectancy_r↑/flat on the accepted set.
-- This table + view are that evidence surface.
--
-- Cohorts (qualified live book only):
--   all_qualified   — everything that passed live gates (product book)
--   bracket_accept  — p_win_bracket >= 0.20 (would survive min_p_win_bracket)
--   bracket_reject  — p_win_bracket <  0.20 (bracket_gate_shadow)
--
-- Dual outcome: closed-only win_rate + pct_expired. Never headline research-stream
-- EOD win%/expiry as product KPI (those stay L3).

CREATE TABLE IF NOT EXISTS nt_live_expectancy_daily (
  day                 DATE NOT NULL,
  cohort              TEXT NOT NULL,
  n                   INTEGER NOT NULL DEFAULT 0,
  n_tp                INTEGER NOT NULL DEFAULT 0,
  n_sl                INTEGER NOT NULL DEFAULT 0,
  n_expired           INTEGER NOT NULL DEFAULT 0,
  win_rate_closed     DOUBLE PRECISION,
  pct_expired         DOUBLE PRECISION,
  expectancy_r        DOUBLE PRECISION,
  avg_p_dir           DOUBLE PRECISION,
  avg_p_win_bracket   DOUBLE PRECISION,
  bias_dir            DOUBLE PRECISION,
  computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, cohort)
);

ALTER TABLE nt_live_expectancy_daily
  DROP CONSTRAINT IF EXISTS nt_live_expectancy_daily_cohort_check;
ALTER TABLE nt_live_expectancy_daily
  ADD CONSTRAINT nt_live_expectancy_daily_cohort_check
  CHECK (cohort = ANY (ARRAY['all_qualified', 'bracket_accept', 'bracket_reject']));

COMMENT ON TABLE nt_live_expectancy_daily IS
  'P2 live expectancy evidence (migration 163). Daily cohort metrics on the QUALIFIED book only. Flip rule: p_win_in_ev_mode→live only after bracket_accept shows expired%↓ AND expectancy_r↑/flat vs all_qualified over ≥7d shadow at min_p_win_bracket=0.20. Snapshot table survives eod_retention of nt_signal_sim.';

COMMENT ON COLUMN nt_live_expectancy_daily.expectancy_r IS
  'win_rate_closed * avg_rr_winners - (1 - win_rate_closed) — same formula as nt_calibration_bias_by_bucket.expectancy_r.';
COMMENT ON COLUMN nt_live_expectancy_daily.bias_dir IS
  'avg_p_dir - win_rate_closed on closed-only episodes (directional honesty).';

ALTER TABLE nt_live_expectancy_daily ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_live_expectancy_daily;
CREATE POLICY "Service role full access" ON nt_live_expectancy_daily
  USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "anon read nt_live_expectancy_daily" ON nt_live_expectancy_daily;
CREATE POLICY "anon read nt_live_expectancy_daily" ON nt_live_expectancy_daily
  FOR SELECT USING (true);
GRANT SELECT ON nt_live_expectancy_daily TO anon;
GRANT ALL ON nt_live_expectancy_daily TO service_role;

-- Live rollup over the snapshot (7d / 28d) + a thin day-level view.
CREATE OR REPLACE VIEW v_p_win_bracket_live_expectancy AS
SELECT
  cohort,
  '7d'::text AS window,
  sum(n)::int AS n,
  sum(n_tp)::int AS n_tp,
  sum(n_sl)::int AS n_sl,
  sum(n_expired)::int AS n_expired,
  CASE WHEN (sum(n_tp) + sum(n_sl)) > 0
       THEN sum(n_tp)::float / (sum(n_tp) + sum(n_sl))
  END AS win_rate_closed,
  CASE WHEN sum(n) > 0 THEN sum(n_expired)::float / sum(n) END AS pct_expired,
  avg(expectancy_r) AS expectancy_r_avg,
  avg(avg_p_dir) AS avg_p_dir,
  avg(avg_p_win_bracket) AS avg_p_win_bracket,
  avg(bias_dir) AS bias_dir
FROM nt_live_expectancy_daily
WHERE day >= (CURRENT_DATE - 7)
GROUP BY cohort
UNION ALL
SELECT
  cohort,
  '28d'::text AS window,
  sum(n)::int,
  sum(n_tp)::int,
  sum(n_sl)::int,
  sum(n_expired)::int,
  CASE WHEN (sum(n_tp) + sum(n_sl)) > 0
       THEN sum(n_tp)::float / (sum(n_tp) + sum(n_sl))
  END,
  CASE WHEN sum(n) > 0 THEN sum(n_expired)::float / sum(n) END,
  avg(expectancy_r),
  avg(avg_p_dir),
  avg(avg_p_win_bracket),
  avg(bias_dir)
FROM nt_live_expectancy_daily
WHERE day >= (CURRENT_DATE - 28)
GROUP BY cohort;

COMMENT ON VIEW v_p_win_bracket_live_expectancy IS
  'Flip-evidence rollup (7d/28d) over nt_live_expectancy_daily. Compare bracket_accept vs all_qualified: want expired%↓ and expectancy_r↑/flat before p_win_in_ev_mode: live. Do not flip on research-stream EOD win%.';

GRANT SELECT ON v_p_win_bracket_live_expectancy TO anon, authenticated;
