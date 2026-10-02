-- Migration 192: calibration cutover — ignore pre-cutover evidence
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHY
-- ───
-- `eod_resolver_basis` flipped from `close_walk` to `intrabar` (2026-10-02,
-- worklog/20261002_SIGNAL_QUALITY_PLAN.md §3.1). The flip CHANGES OUTCOME
-- SEMANTICS: `tp_hit` now means "intrabar high/low touched TP first" instead of
-- "a 1h CLOSE passed TP". Measured on 60d of history, that re-labels a
-- substantial share of rows — the survivable base rate is 0.057–0.069 intrabar
-- vs 0.2728 close-walk, and the intrabar bracket rate is itself 0.0265 lower.
--
-- Rows resolved before the flip were labelled under the old basis. Mixing them
-- with post-flip rows corrupts every downstream calibration number: bias,
-- Brier, kelly shrink, and the EOD correction verdicts all compare
-- `predicted_p_win` against a win rate computed from a different event.
--
-- WHAT THIS DOES
-- ──────────────
-- Adds a **cutover timestamp** as a single tunable source of truth, and a
-- view `v_calibration_records_current` that exposes ONLY post-cutover records.
-- The bias readers switch to that view, so calibration restarts fresh and
-- accrues clean evidence going forward.
--
-- Deliberately NOT a DELETE. The pre-cutover rows are the historical record of
-- what the close-walk basis produced — needed for the before/after comparison
-- and for any future re-derivation. They are excluded, not destroyed.
--
-- WHY A TABLE ROW, NOT A HARDCODED CONSTANT
-- ──────────────────────────────────────────
-- The cutover instant must be the moment the flip actually deployed, which is
-- not knowable when this migration is written. A single row in
-- `nt_calibration_cutover` lets an operator set it exactly once at deploy time
-- (`UPDATE ... SET cutover_at = now()`), and every reader derives from it — no
-- code change, no redeploy, and no chance of two readers disagreeing.
--
-- The view returns ALL rows when the table is empty (no cutover configured),
-- so this migration is inert until the timestamp is set. That is the safe
-- default: applying it changes nothing.
--
-- Read-only for the application: the app never writes this table.

-- ─── 1. The cutover marker ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS nt_calibration_cutover (
  id          INT PRIMARY KEY DEFAULT 1,
  cutover_at  TIMESTAMPTZ NOT NULL,
  basis       TEXT NOT NULL DEFAULT 'intrabar',
  note        TEXT,
  CONSTRAINT nt_calibration_cutover_singleton CHECK (id = 1)
);

COMMENT ON TABLE nt_calibration_cutover IS
  'Singleton row (id=1) marking the instant calibration evidence switched basis. Records with entered_at < cutover_at are excluded from bias/Brier/enforcement reads via v_calibration_records_current. Set cutover_at to the deploy time of the eod_resolver_basis flip. Empty table = no cutover = all rows visible (inert).';
COMMENT ON COLUMN nt_calibration_cutover.cutover_at IS
  'Records with entered_at < cutover_at are pre-cutover and excluded from calibration reads. Set to the moment the intrabar resolver deployed.';
COMMENT ON COLUMN nt_calibration_cutover.basis IS
  'Which TP/SL basis the post-cutover rows were resolved under. Informational — lets a future reader know what the evidence after this instant means.';

-- ─── 2. The filtered view ─────────────────────────────────────────────────

CREATE OR REPLACE VIEW v_calibration_records_current AS
SELECT r.*
FROM nt_calibration_records r
WHERE NOT EXISTS (
  SELECT 1 FROM nt_calibration_cutover c
  WHERE c.id = 1 AND r.entered_at < c.cutover_at
);

COMMENT ON VIEW v_calibration_records_current IS
  'nt_calibration_records restricted to post-cutover evidence (entered_at >= nt_calibration_cutover.cutover_at). Returns ALL rows when no cutover is configured, so it is a drop-in replacement for the base table and is inert before the flip. The bias readers (fetch_calibration_bias / fetch_calibration_bias_raw) read this; the resolver still WRITES to the base table.';

-- ─── 2b. Compose the cutover filter into the tiered view ──────────────────
--
-- `v_calibration_records_tiered` (migration 171) is defined on the BASE table
-- and is selected when `evidence_tiers` is passed. Composing the two views would
-- double-join `nt_signal_sim` for no benefit, so the cutover predicate is added
-- to the tiered view directly. Same column list, same tier logic — only the
-- WHERE clause is new, so every existing reader is unaffected (the predicate is
-- vacuously true while `nt_calibration_cutover` is empty).

CREATE OR REPLACE VIEW v_calibration_records_tiered
WITH (security_invoker = on) AS
SELECT
  c.*,
  CASE
    WHEN s.gate_outcome IN ('qualified', 'suppressed', 'gate_fail') THEN s.gate_outcome
    WHEN s.signal_id IS NULL THEN 'unknown'
    WHEN s.qualified THEN 'qualified'
    WHEN s.regime_conf >= 0.60
     AND s.effective_kelly >= 0.02
     AND s.ev > 0
     AND s.tp_bricks::numeric / NULLIF(s.sl_bricks, 0) >= 1.5
     AND coalesce(s.p_win_dir, s.p_win) >= 0.50 THEN 'suppressed'
    ELSE 'gate_fail'
  END AS evidence_tier,
  CASE
    WHEN s.gate_outcome IN ('qualified', 'suppressed', 'gate_fail') THEN 'stamp'
    WHEN s.signal_id IS NULL THEN 'missing'
    ELSE 'recon'
  END AS tier_src,
  s.source AS sim_source
FROM nt_calibration_records c
LEFT JOIN nt_signal_sim s ON s.signal_id = c.signal_id
WHERE NOT EXISTS (
  SELECT 1 FROM nt_calibration_cutover k
  WHERE k.id = 1 AND c.entered_at < k.cutover_at
);

COMMENT ON VIEW v_calibration_records_tiered IS
  'nt_calibration_records + evidence_tier (qualified|suppressed|gate_fail|unknown) of the producing signal, joined on signal_id. tier_src: stamp (gate_outcome) | recon (validated reconstruction) | missing. Read by fetch_calibration_bias(_raw)(evidence_tiers=...) for the calibration_evidence_pool_mode shadow/live bias (migration 171, Kairos V3 §14.3). Since migration 192 it also applies the post-cutover filter, so the evidence pool and the plain bias path exclude the same pre-cutover rows.';

-- ─── 3. RLS — service role only, same shape as migration 100/113 ──────────

ALTER TABLE nt_calibration_cutover ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Service role full access" ON nt_calibration_cutover;
CREATE POLICY "Service role full access" ON nt_calibration_cutover
  FOR ALL TO service_role USING (true) WITH CHECK (true);

GRANT SELECT ON v_calibration_records_current TO service_role;

-- ─── 4. Operational note ──────────────────────────────────────────────────
--
-- Applying this migration alone changes NOTHING (empty table -> view passes
-- every row). To activate the cutover, at the moment the flip deploys:
--
--   INSERT INTO nt_calibration_cutover (id, cutover_at, basis, note)
--   VALUES (1, now(), 'intrabar', 'eod_resolver_basis flip')
--   ON CONFLICT (id) DO UPDATE
--     SET cutover_at = EXCLUDED.cutover_at,
--         basis      = EXCLUDED.basis,
--         note       = EXCLUDED.note;
--
-- To roll the cutover back (re-include history), DELETE the row.
