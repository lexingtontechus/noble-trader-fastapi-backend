-- 186_nt_signal_sim_timesfm_quantiles.sql  (2026-09-28)
--
-- P1.3 TimesFM quantile capture (worklog/kairos/20260928_TIMESFM_QUANTILE_CAPTURE.md).
-- The univariate /forecast response already returns quantile_forecast
-- (shape [series][horizon][q]); the orchestrator previously discarded it.
-- Persist the p10/p50/p90 band + derived research features so P1.3 can
-- run IC / ΔBrier on y_acc without a second TimesFM call.
--
-- Research stamps ONLY — never gates, Kelly, or EV (same rule as
-- kairos_rank_score, migration 172).

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS timesfm_q_lo   double precision,
  ADD COLUMN IF NOT EXISTS timesfm_q_mid  double precision,
  ADD COLUMN IF NOT EXISTS timesfm_q_hi   double precision,
  ADD COLUMN IF NOT EXISTS timesfm_q_spread double precision,
  ADD COLUMN IF NOT EXISTS timesfm_q_pos  double precision;

COMMENT ON COLUMN nt_signal_sim.timesfm_q_lo IS
  'TimesFM quantile p10 at horizon step 0 (price units). P1.3 research stamp.';
COMMENT ON COLUMN nt_signal_sim.timesfm_q_mid IS
  'TimesFM quantile p50 at horizon step 0 (price units). P1.3 research stamp.';
COMMENT ON COLUMN nt_signal_sim.timesfm_q_hi IS
  'TimesFM quantile p90 at horizon step 0 (price units). P1.3 research stamp.';
COMMENT ON COLUMN nt_signal_sim.timesfm_q_spread IS
  '(q90-q10)/last_price — forecast uncertainty band. Research only.';
COMMENT ON COLUMN nt_signal_sim.timesfm_q_pos IS
  'Where last_price sits in [q10,q90] (0=at q10, 1=at q90). Research only.';

-- POST-RUN VERIFICATION:
--   SELECT count(timesfm_q_lo), count(*) FROM nt_signal_sim
--   WHERE ts > now() - interval '1 day' AND source = 'sweep-light';
