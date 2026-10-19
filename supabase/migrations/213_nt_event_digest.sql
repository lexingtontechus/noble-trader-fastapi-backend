-- Migration 213: nt_event_digest — event-triggered LLM digest results
-- Cross-asset by nature (one row per event, not per symbol).
-- Design: docs/scopes/20261019_MACRO_EVENT_ODDS_LAYER_ASSESSMENT.md §6.2

CREATE TABLE IF NOT EXISTS nt_event_digest (
  digest_ts       TIMESTAMPTZ NOT NULL,
  event_key       TEXT NOT NULL,
  odds_snapshot   JSONB NOT NULL,
  stance          TEXT CHECK (stance IN ('risk_on', 'risk_off', 'neutral')),
  vol_expansion   TEXT CHECK (vol_expansion IN ('low', 'medium', 'high')),
  confidence      DOUBLE PRECISION,
  model_provider  TEXT,
  PRIMARY KEY (digest_ts, event_key)
);

CREATE INDEX IF NOT EXISTS nt_event_digest_event_ts_idx
  ON nt_event_digest (event_key, digest_ts DESC);

ALTER TABLE nt_event_digest ENABLE ROW LEVEL SECURITY;
