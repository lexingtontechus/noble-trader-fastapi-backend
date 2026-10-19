-- Migration 212: nt_event_odds — market-implied event probabilities
-- Append-only history table for prediction-market odds snapshots.
-- Design: docs/scopes/20261019_MACRO_EVENT_ODDS_LAYER_ASSESSMENT.md §6.2

CREATE TABLE IF NOT EXISTS nt_event_odds (
  id              BIGSERIAL PRIMARY KEY,
  poll_ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  source          TEXT NOT NULL CHECK (source IN ('kalshi', 'polymarket')),
  event_key       TEXT NOT NULL,              -- e.g. 'FOMC_2026-10-19'
  event_ts        TIMESTAMPTZ,               -- scheduled decision time
  market_id       TEXT,
  outcome         TEXT NOT NULL,             -- 'hike_25bp' | 'hold' | ...
  implied_prob    DOUBLE PRECISION,
  bid             DOUBLE PRECISION,
  ask             DOUBLE PRECISION,
  volume          DOUBLE PRECISION,
  open_interest   DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS nt_event_odds_event_poll_idx
  ON nt_event_odds (event_key, poll_ts DESC);

-- Service-role RLS (matches migration 124/181/182 pattern)
ALTER TABLE nt_event_odds ENABLE ROW LEVEL SECURITY;

-- "latest state" = max(poll_ts) per event_key/outcome — a view, not a table.
CREATE OR REPLACE VIEW v_event_odds_latest AS
SELECT DISTINCT ON (event_key, outcome)
  id, poll_ts, source, event_key, event_ts, outcome,
  implied_prob, bid, ask, volume, open_interest
FROM nt_event_odds
ORDER BY event_key, outcome, poll_ts DESC;
