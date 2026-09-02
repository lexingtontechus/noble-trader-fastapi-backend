-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 132: nt_regime_debounce_shadow
--
-- Fix 2 of the regime-label-stability scope (worklog
-- 20260901_REGIME_LABEL_STABILITY_SCOPE.md, 2026-09-01): hysteresis on
-- RegimeSnapshot.regime_label (RegimeLabelDebouncer in regime_engine.py),
-- gated by regime_label_debounce_mode in config/feature_flags.yaml.
-- shadow mode computes the debounced label every tick but only persists
-- the running comparison here — direction selection and regime-shift
-- detection keep reading the RAW label until a human reviews this table
-- and flips the mode to live.
--
-- Column style follows migration 123 (nt_trend_probs_gate_shadow): nt_
-- prefix, snake_case, one running row per symbol (fetch-then-upsert),
-- service-role-only RLS, updated_at included in the payload from the
-- start.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_regime_debounce_shadow (
  symbol             TEXT PRIMARY KEY,
  n_compared         INTEGER NOT NULL DEFAULT 0,
  n_differed         INTEGER NOT NULL DEFAULT 0,
  last_raw_label     TEXT,
  last_debounced_label TEXT,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_regime_debounce_shadow IS
  'Running per-symbol comparison between the raw and debounced (hysteresis-filtered) regime_label, accumulated whenever regime_label_debounce_mode=shadow. n_differed/n_compared = fraction of ticks where the debounced label would have differed from the raw one. Diagnostic only — read manually before deciding whether to flip regime_label_debounce_mode to live.';
COMMENT ON COLUMN nt_regime_debounce_shadow.n_differed IS
  'Running count of ticks where last_debounced_label != last_raw_label at that tick.';

ALTER TABLE nt_regime_debounce_shadow ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_regime_debounce_shadow;
CREATE POLICY "Service role full access" ON nt_regime_debounce_shadow
  FOR ALL TO service_role USING (true) WITH CHECK (true);
