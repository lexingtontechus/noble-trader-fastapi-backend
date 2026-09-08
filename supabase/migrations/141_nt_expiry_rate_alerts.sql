-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 141: nt_expiry_rate_alerts
--
-- Priority #3, worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md.
-- Cooldown/dedup state for regime_platform/sweep_orchestrator/
-- expiry_rate_alert.py: one row per symbol, tracking the last time a
-- high-expiry-rate Discord alert was sent so a persisting condition
-- doesn't re-alert every day (config/feature_flags.yaml's
-- expiry_rate_alert.cooldown_hours governs the gap).
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_expiry_rate_alerts (
  symbol       TEXT PRIMARY KEY,
  last_alert_at TIMESTAMPTZ NOT NULL,
  expired_pct  DOUBLE PRECISION NOT NULL,
  resolved_n   INTEGER NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_expiry_rate_alerts IS
  'Cooldown/dedup state for the expiry-rate Discord alert (worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md priority #3) -- one row per symbol, last_alert_at gates re-alerting via expiry_rate_alert.cooldown_hours (config/feature_flags.yaml).';

ALTER TABLE nt_expiry_rate_alerts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_expiry_rate_alerts;
CREATE POLICY "Service role full access" ON nt_expiry_rate_alerts
  FOR ALL TO service_role USING (true) WITH CHECK (true);
