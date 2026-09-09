-- 2026-09-09: nt_symbol_calibration_state -- Option A of the
-- nt_eod_actions state/history split (worklog
-- 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16 item #6).
--
-- Splits two roles that nt_eod_actions was serving at once:
--   - AUDIT (unchanged, stays on nt_eod_actions): a permanent history of
--     every calibration-bias assessment ever made, at any cadence, by any
--     of the 4 writers (eod_correction nightly/intraday, drift_monitor,
--     regime_shift_alert). Multiple writers append; multiple things read
--     it for history (e.g. _stagnation_streak's own bias-history lookup).
--     Nothing about this table's role changes.
--   - CURRENT STATE (new, this table): "what is the CURRENTLY enforceable
--     calibration verdict for symbol X, right now" -- exactly ONE row per
--     symbol, upserted (never inserted-and-orphaned), written through
--     EXACTLY ONE code path (supabase_client.upsert_calibration_state) so
--     the three verdict-computing passes (eod_correction nightly,
--     eod_correction intraday, drift_monitor) can never interleave/
--     contaminate each other's writes the way they could (and did --
--     see worklog for the two confirmed incidents) when every reader had
--     to reconstruct "current state" via a "latest row matching filters"
--     query against a shared multi-writer log.
--
-- apply_calibration_adjustment (the live enforcement consumer) and
-- drift_monitor's own freshness guard both read ONLY this table now --
-- neither queries nt_eod_actions for "what's currently active" anymore.

CREATE TABLE IF NOT EXISTS nt_symbol_calibration_state (
  symbol                    TEXT PRIMARY KEY,
  action                    TEXT NOT NULL DEFAULT 'none'
                              CHECK (action IN ('none', 'bayesian_penalty', 'stagnation_kill_switch')),
  bias                      NUMERIC,
  status                    TEXT
                              CHECK (status IS NULL OR status IN ('CALIBRATED', 'OVERCONFIDENT', 'UNDERCONFIDENT')),
  n_records                 INTEGER,
  kelly_reduction_override  NUMERIC,
  stagnation_tier           INTEGER NOT NULL DEFAULT 0,
  source                    TEXT NOT NULL,
  detail                    JSONB NOT NULL DEFAULT '{}'::jsonb,
  computed_at               TIMESTAMPTZ NOT NULL,
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_symbol_calibration_state IS
  'THE single current-state record per symbol for calibration enforcement -- one row, one writer (supabase_client.upsert_calibration_state), many readers. Separated from nt_eod_actions (the permanent multi-writer audit log) 2026-09-09 to structurally eliminate the "reconstruct current state from the latest matching log row" fragility that caused two confirmed production incidents. See worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16.';

ALTER TABLE nt_symbol_calibration_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_symbol_calibration_state;
CREATE POLICY "Service role full access" ON nt_symbol_calibration_state
  FOR ALL TO service_role USING (true) WITH CHECK (true);
GRANT SELECT ON nt_symbol_calibration_state TO anon, authenticated;
