-- Migration 194: p_clef shadow-stamp columns on nt_signal_sim
-- ═══════════════════════════════════════════════════════════════════════════
-- WHY
-- ───
-- Clef-Flash (Cloudflare Workers AI `@cf/cloudflare/clef-flash`) is a
-- structured-decision model: it returns one logit per option per question,
-- softmax-normalised — no free text, no extract_json() fragility. It is
-- REPURPOSED from a retired EV-blend source to a shadow research stamp
-- (docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md and
-- 20261004_P_CLEF_SHADOW_STAMP_REQUIREMENTS.md).
--
-- These three columns record what the model answers for a signal's OWN
-- context (symbol/direction/regime/bracket) so its discrimination can be
-- measured offline against the existing bracket probability (p_win_dir).
--
-- RESEARCH ONLY. Nothing reads these columns to make a decision:
--   * never added to P_WIN_WEIGHTS_FULL
--   * never passed to compute_ev()
--   * never touches qualified / published / gate_outcome / kelly / SL / TP
-- The same shadow-stamp convention as raw_p_regime / timesfm_q_* on this
-- table. All three nullable, no default — absent when clef_flash_mode is
-- `off`, when the signal is not a light-sweep qualifiable tick, or when the
-- call fails.
--
-- COLUMNS
-- ───────
--   p_clef_dir     P(model agrees with the signal's direction) — the
--                  discrimination column, compared against p_win_dir.
--   p_clef_wait    P(model says "wait") — the abstention column, so a
--                  model that abstains on most signals is visible.
--   p_clef_symbol  the model slug actually used (provenance).
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS p_clef_dir    DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS p_clef_wait   DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS p_clef_symbol TEXT;

COMMENT ON COLUMN nt_signal_sim.p_clef_dir IS
  'Clef-Flash shadow stamp: P(model agrees with the signal direction). Research only — never an EV source, never a gate. Compare against p_win_dir.';
COMMENT ON COLUMN nt_signal_sim.p_clef_wait IS
  'Clef-Flash shadow stamp: P(model says "wait"). Abstention column — makes a non-discriminating model visible. Research only.';
COMMENT ON COLUMN nt_signal_sim.p_clef_symbol IS
  'Clef-Flash model slug used for the shadow stamp (e.g. @cf/cloudflare/clef-flash). Provenance for a future model swap. Research only.';

-- ─── Post-run verification ─────────────────────────────────────────────────
--   SELECT column_name, data_type, col_description('nt_signal_sim'::regclass,
--          ordinal_position)
--   FROM information_schema.columns
--   WHERE table_name = 'nt_signal_sim' AND column_name LIKE 'p_clef%';
--   -- expect 3 rows: p_clef_dir (double precision), p_clef_wait (double precision),
--   -- p_clef_symbol (text)
