-- 2026-09-09: shadow-tracking table for the signal-stream meta-regime
-- model (regime_platform/sweep_orchestrator/meta_regime_model.py).
--
-- Direct response to "what happened to all the shadow mode tracking?" --
-- the model and its local SQLite lookback cache were built, but nothing
-- ever wired signal_stream_cache.append_tick() into the live sweep, and
-- no shadow table existed to persist a running comparison the way every
-- OTHER shadow mechanism this session (nt_heavy_sweep_deadline_aware_
-- shadow, nt_regime_debounce_shadow, nt_markov_source_shadow, ...)
-- already does. "Weeks of shadow-mode data" was never going to happen on
-- its own without this table existing to accumulate it in.
--
-- One row per symbol, upserted every sweep tick once
-- signal_stream_meta_model_mode != off and enough local history exists
-- to fit (MIN_TICKS_TO_FIT=50). Frequent (every tick), not a "come back
-- in weeks" checkpoint -- the state/probabilities are visible and
-- queryable from the very first fit onward, same as every other shadow
-- table.

CREATE TABLE IF NOT EXISTS nt_signal_stream_meta_shadow (
  symbol                  TEXT PRIMARY KEY,
  state                   TEXT NOT NULL,
  coherent_prob           DOUBLE PRECISION NOT NULL,
  noisy_prob              DOUBLE PRECISION NOT NULL,
  ticks_in_current_state  INTEGER NOT NULL,
  since_ts                DOUBLE PRECISION,
  n_transitions_total     INTEGER NOT NULL,
  n_ticks_fit             INTEGER NOT NULL,
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_signal_stream_meta_shadow IS
  'Shadow tracking for the signal-stream meta-regime model (worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md sections 3-5, 11) -- one row per symbol, updated every sweep tick once signal_stream_meta_model_mode != off. state/coherent_prob/noisy_prob are the models current read; ticks_in_current_state/since_ts/n_transitions_total come from summarize_since_last_transition (event-driven, not a fixed window). Diagnostic only while mode=shadow -- nothing reads this for a live decision yet.';

ALTER TABLE nt_signal_stream_meta_shadow ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_signal_stream_meta_shadow;
CREATE POLICY "Service role full access" ON nt_signal_stream_meta_shadow
  FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Also readable by the webapp (same read-only pattern as the scorecard
-- views) so shadow-mode progress can be checked without SQL access.
GRANT SELECT ON nt_signal_stream_meta_shadow TO anon, authenticated;
