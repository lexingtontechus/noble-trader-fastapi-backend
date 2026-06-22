-- ═══════════════════════════════════════════════════════════════
-- 024: EV calibration columns + L2 runtime config seed data
--
-- 1. Rename p_imbalance → p_ext_signal in nt_calibration_records
-- 2. Add calibration quality columns
-- 3. Seed L2 and EV config keys into nt_system_config
-- ═══════════════════════════════════════════════════════════════

-- ─── 1. Rename column in nt_calibration_records ──────────────
-- p_imbalance was renamed to p_ext_signal to reflect that this
-- slot accepts any external directional vote, not just L2.

ALTER TABLE nt_calibration_records
  RENAME COLUMN p_imbalance TO p_ext_signal;

COMMENT ON COLUMN nt_calibration_records.p_ext_signal
  IS 'External directional signal vote at entry (from L2 order book, sentiment, etc.)';

-- ─── 2. Add calibration quality columns ───────────────────────

ALTER TABLE nt_calibration_records
  ADD COLUMN IF NOT EXISTS predicted_p_win_raw DOUBLE PRECISION;
COMMENT ON COLUMN nt_calibration_records.predicted_p_win_raw
  IS 'P_win before Platt scaling / isotonic calibration';

ALTER TABLE nt_calibration_records
  ADD COLUMN IF NOT EXISTS calibration_brier DOUBLE PRECISION;
COMMENT ON COLUMN nt_calibration_records.calibration_brier
  IS 'Brier score for this prediction (0=perfect, 1=worst)';

-- (p_ext_signal was already added via RENAME COLUMN above)

-- ─── 3. Seed L2 + EV config keys ─────────────────────────────
-- These are upserted so they survive schema re-applies.

INSERT INTO nt_system_config (key, value, value_type, category, description, default_value)
VALUES
  ('l2.enabled',              'true',          'bool', 'l2',        'Master switch for L2 order book pipeline',                'true'),
  ('l2.levels',               '5',             'int',  'l2',        'Number of depth levels to use for imbalance',              '5'),
  ('l2.level_weights',        '[0.40,0.25,0.15,0.12,0.08]', 'json', 'l2', 'Level weights for multi-level imbalance',       '[0.40,0.25,0.15,0.12,0.08]'),
  ('l2.imbalance_threshold',  '0.15',          'float','l2',        'Min |imbalance| to trigger buy/sell signal',              '0.15'),
  ('l2.persistence_window',   '20',            'int',  'l2',        'Sliding window size for persistence tracking',             '20'),
  ('ev.calibration_enabled',  'true',          'bool', 'ev',        'Enable P_win calibration tracking',                        'true'),
  ('ev.calibration_min_records', '20',         'int',  'ev',        'Min records before calibration bias is meaningful',        '20'),
  ('ev.calibration_over_threshold', '0.05',    'float','ev',        '|bias| >= this triggers over/underconfident flag',         '0.05')
ON CONFLICT (key) DO NOTHING;
