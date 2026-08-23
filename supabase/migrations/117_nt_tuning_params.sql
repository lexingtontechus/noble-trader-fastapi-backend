-- ═══════════════════════════════════════════════════════════════
-- Migration 117: nt_tuning_params — parameter tuning audit ledger
-- (2026-08-23, Layer 0 of the agent-driven EV engine tuning registry)
--
-- See worklog/PARAMETER_TUNING_PLAYBOOK.md §2 for the full design.
--
-- Read precedence for a tunable parameter: config file (fast, local,
-- hot-reloaded) -> this table (durable audit trail + cross-instance
-- fallback) -> settings.py factory default. This table is append-only,
-- same shape as nt_eod_actions: one row per write, "latest row per
-- param_name" is the current value. That gives the audit trail for free
-- (unlike the config file, which is NOT git-tracked-in-practice — see
-- the playbook's correction note on config/brick_params.yaml).
--
-- value/previous_value are JSONB (not DOUBLE PRECISION) so this table can
-- hold numeric, boolean, or small-object tunables without a schema change.
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_tuning_params (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  param_name      TEXT NOT NULL,
  value           JSONB NOT NULL,
  previous_value  JSONB,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by      TEXT,                    -- e.g. 'agent:layer1-tuner', 'agent:claude-session', 'operator'
  reason          TEXT,                    -- free-text rationale for this write
  clamped         BOOLEAN NOT NULL DEFAULT false,   -- true if PARAM_BOUNDS forced a different value than requested
  rate_limited    BOOLEAN NOT NULL DEFAULT false,   -- true if the max-step-per-cycle rule capped the move
  cycle_result    TEXT,                    -- 'pending' | 'improved' | 'regressed' | 'neutral' — filled in by the monitoring window (§4.8)
  reverted        BOOLEAN NOT NULL DEFAULT false     -- true if this row is itself an automatic rollback of a prior write
);

CREATE INDEX IF NOT EXISTS idx_tuning_params_name_ts
  ON nt_tuning_params (param_name, ts DESC);

COMMENT ON TABLE nt_tuning_params IS
  'Append-only audit ledger for autonomous/agent-driven parameter tuning (Layer 0 of the tuning registry). Latest row per param_name is the current value. See worklog/PARAMETER_TUNING_PLAYBOOK.md.';
COMMENT ON COLUMN nt_tuning_params.value IS
  'JSONB so numeric/boolean/small-object tunables share one table without a schema change.';
COMMENT ON COLUMN nt_tuning_params.cycle_result IS
  'Set by the post-deployment monitoring window (playbook §4.8): whether the tracked metric improved/regressed/stayed neutral after this write.';

-- ─── Row Level Security ─────────────────────────────────────────────────
-- Same policy shape as nt_eod_actions (migration 102/113): service role
-- (backend) only, RLS enabled defensively even though service_role
-- bypasses RLS.

ALTER TABLE nt_tuning_params ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_tuning_params;
CREATE POLICY "Service role full access" ON nt_tuning_params
  FOR ALL TO service_role USING (true) WITH CHECK (true);
