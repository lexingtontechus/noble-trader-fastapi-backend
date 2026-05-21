-- Phase 5: P&L Alert Thresholds — Persistent storage for per-user P&L alert rules
-- Replaces in-memory dict that was lost on server restart

CREATE TABLE IF NOT EXISTS pnl_alert_thresholds (
    id                VARCHAR(36) PRIMARY KEY,       -- UUID
    user_id           VARCHAR(255) NOT NULL,
    metric            VARCHAR(30) NOT NULL,          -- day_pnl, unrealized_pnl, drawdown_pct, var_breach, equity_change_pct
    operator          VARCHAR(20) NOT NULL,          -- lt, gt, lte, gte, crosses_below, crosses_above
    value             DOUBLE PRECISION NOT NULL,
    severity          VARCHAR(10) NOT NULL DEFAULT 'warning',  -- info, warning, critical
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    cooldown_minutes  INTEGER NOT NULL DEFAULT 15,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_triggered    TIMESTAMPTZ
);

-- Row-Level Security
ALTER TABLE pnl_alert_thresholds ENABLE ROW LEVEL SECURITY;

-- Service role (using apikey) can perform all operations
CREATE POLICY "Service role full access" ON pnl_alert_thresholds
    FOR ALL USING (true) WITH CHECK (true);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_pnl_alert_user_id ON pnl_alert_thresholds (user_id);
CREATE INDEX IF NOT EXISTS idx_pnl_alert_enabled ON pnl_alert_thresholds (user_id, enabled) WHERE enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_pnl_alert_metric ON pnl_alert_thresholds (metric);

-- Add comment
COMMENT ON TABLE pnl_alert_thresholds IS 'Phase 5: P&L alert thresholds for real-time risk monitoring. Persists across server restarts.';
