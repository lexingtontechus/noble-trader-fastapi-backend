-- 207_clef_archetype_and_stance.sql
--
-- 2026-10-10 — R4 (per-signal archetype classifier stamp) + R5 (multi-asset
-- stance table).
-- Scope: docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md §5.
--
-- R4 adds three columns to nt_signal_sim: the Clef-Flash archetype read
-- (Role A — which SETUP TYPE fits), its confidence, and whether it agrees
-- with the R1 deterministic router's own archetype stamp. Measurement-only:
-- R4's bar is whether the model's archetype read discriminates outcomes /
-- agrees with the router better than chance. Never an EV source / gate.
--
-- R5 adds nt_clef_stance: one cross-asset buy/sell/hold stance row per
-- symbol (Role B — the portfolio overlay), upserted keyed on symbol like
-- nt_macro_regime. Shadow measurement only.
--
-- ADDITIVE + NULLABLE, no backfill. Both are shadow; historical rows never
-- carried these reads.

-- ── R4: per-signal archetype stamp ─────────────────────────────────────────
ALTER TABLE nt_signal_sim
    ADD COLUMN IF NOT EXISTS clef_archetype TEXT,
    ADD COLUMN IF NOT EXISTS clef_archetype_conf DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS clef_archetype_agrees BOOLEAN;

COMMENT ON COLUMN nt_signal_sim.clef_archetype IS
    'Clef-Flash archetype classifier top label (R4 Role A, shadow): '
    'momentum | mean_reversion | range_fade | breakout | stand_by. '
    'Measurement-only vs the R1 router; never an EV source / gate. NULL '
    'when clef_flash_mode=off, Worker unconfigured, or router errored.';
COMMENT ON COLUMN nt_signal_sim.clef_archetype_conf IS
    'Clef-Flash archetype classifier max probability (confidence). R4 shadow.';
COMMENT ON COLUMN nt_signal_sim.clef_archetype_agrees IS
    'Whether the Clef-Flash archetype top label equals the R1 deterministic '
    'router archetype for the same signal. NULL when the router produced '
    'nothing. R4 shadow measurement.';

-- ── R5: multi-asset stance table ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nt_clef_stance (
    symbol      TEXT PRIMARY KEY,
    stance      TEXT NOT NULL,               -- buy | sell | hold
    confidence  DOUBLE PRECISION,
    probabilities JSONB,                      -- {buy:.., sell:.., hold:..}
    regime      TEXT,
    macro_label TEXT,
    trend_label TEXT,
    momentum_24h DOUBLE PRECISION,
    vol_ratio   DOUBLE PRECISION,
    deviation_z DOUBLE PRECISION,
    model       TEXT,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_clef_stance IS
    'R5 multi-asset buy/sell/hold stance via Clef-Flash (Role B, shadow). '
    'One row per symbol, upserted keyed on symbol. Portfolio-level overlay, '
    'measurement-only — never an EV source / gate. Scope '
    '20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md §5 Role B.';

CREATE INDEX IF NOT EXISTS idx_clef_stance_computed
    ON nt_clef_stance (computed_at DESC);

-- PostgREST must see the new column + table immediately (stale schema cache).
NOTIFY pgrst, 'reload schema';
