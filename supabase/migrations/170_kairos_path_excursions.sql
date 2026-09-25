-- ═══════════════════════════════════════════════════════════════════════════════
-- 170_kairos_path_excursions.sql  (2026-09-25, branch kairos)
--
-- Fixed-horizon, geometry-free path risk for every light-sweep buy/sell signal.
-- Scope: worklog/kairos/20260925_PATH_RISK_AND_REGIME_SURVIVAL.md.
--
--   mae_{h}h_pct / mfe_{h}h_pct (h ∈ 4, 12, 24, 48) — maximum adverse /
--     favourable excursion, % of entry, NON-NEGATIVE, over the h completed
--     1h bars AFTER the entry bar (bar highs/lows). The entry bar itself is
--     excluded because its range includes prices from before the signal. The
--     window ends on the same bar close as drift_{h}h_pct, so the signed
--     drift always lies in [−mae_h, +mfe_h].
--
--   These are NOT the existing nt_signal_sim.mae / mfe columns. Those are
--   walked only until the TP/SL bracket closes, so they depend on the geometry
--   and are excluded from Kairos. These run the full h bars regardless of
--   bracket or outcome.
--
--   Written by kairos.backfill_drift_horizons (hourly kairos-daily job); NULL
--   until bar h exists. Additive, nullable, and no insert path writes them.
--
-- v_kairos_signal_episodes gains the ATR-unit versions mae4..mae48 and
-- mfe4..mfe48, appended after the existing columns. Every other column and
-- all the logic are identical to migration 168.
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS mae_4h_pct  double precision,
  ADD COLUMN IF NOT EXISTS mae_12h_pct double precision,
  ADD COLUMN IF NOT EXISTS mae_24h_pct double precision,
  ADD COLUMN IF NOT EXISTS mae_48h_pct double precision,
  ADD COLUMN IF NOT EXISTS mfe_4h_pct  double precision,
  ADD COLUMN IF NOT EXISTS mfe_12h_pct double precision,
  ADD COLUMN IF NOT EXISTS mfe_24h_pct double precision,
  ADD COLUMN IF NOT EXISTS mfe_48h_pct double precision;

COMMENT ON COLUMN nt_signal_sim.mae_24h_pct IS
  'Kairos path risk: max adverse excursion (>= 0), % of entry, over the 24 completed 1h bars after the entry bar (highs/lows). Fixed horizon — independent of TP/SL. Backfilled hourly (kairos.backfill_drift_horizons). Same for mae_4h/12h/48h.';
COMMENT ON COLUMN nt_signal_sim.mfe_24h_pct IS
  'Kairos path risk: max favourable excursion (>= 0), % of entry, over the 24 completed 1h bars after the entry bar. Fixed horizon — independent of TP/SL. Same for mfe_4h/12h/48h.';

CREATE OR REPLACE VIEW v_kairos_signal_episodes AS
WITH ticks AS (
  SELECT s.*,
         date_trunc('hour', s.ts) AS hr,
         CASE WHEN s.gate_outcome IS NOT NULL THEN s.gate_outcome
              WHEN s.qualified THEN 'qualified'
              ELSE 'unknown' END AS tier_row
  FROM nt_signal_sim s
  WHERE s.source = 'sweep-light'
    AND s.signal IN ('buy', 'sell')
    AND s.ts >= '2026-09-22T12:00:00Z'
),
ranked AS (
  SELECT t.*,
         row_number() OVER (PARTITION BY symbol, signal, hr ORDER BY ts) AS rn,
         min(CASE tier_row WHEN 'qualified' THEN 1 WHEN 'suppressed' THEN 2
                           WHEN 'gate_fail' THEN 3 ELSE 4 END)
           OVER (PARTITION BY symbol, signal, hr) AS best_rank,
         count(*) OVER (PARTITION BY symbol, signal, hr) AS n_ticks,
         bool_or(suppress_reason IS NOT NULL AND suppress_reason LIKE 'cooldown%')
           OVER (PARTITION BY symbol, signal, hr) AS any_cooldown
  FROM ticks t
)
SELECT r.symbol,
       r.signal                              AS direction,
       r.hr,
       r.ts                                  AS first_ts,
       r.n_ticks,
       CASE r.best_rank WHEN 1 THEN 'qualified' WHEN 2 THEN 'suppressed'
                        WHEN 3 THEN 'gate_fail' ELSE 'unknown' END AS tier,
       r.suppress_reason,
       r.any_cooldown,
       r.failed_gates,
       r.regime,
       split_part(r.regime, '_vol_', 1) || '_vol'
         || '|' || CASE WHEN r.regime ILIKE '%bull%' THEN 'bull'
                        WHEN r.regime ILIKE '%bear%' THEN 'bear'
                        ELSE 'flat' END  AS cell,
       r.entry_price,
       r.atr,
       r.p_win,
       r.regime_conf,
       r.effective_kelly,
       r.ev,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.drift_1h_pct  / 100.0 * r.entry_price / r.atr END AS m1,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.drift_4h_pct  / 100.0 * r.entry_price / r.atr END AS m4,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.drift_12h_pct / 100.0 * r.entry_price / r.atr END AS m12,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.drift_24h_pct / 100.0 * r.entry_price / r.atr END AS m24,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.drift_48h_pct / 100.0 * r.entry_price / r.atr END AS m48,
       r.rel_volume_1h,
       r.clv_1h,
       r.efficiency_24h,
       r.atr_ratio_run,
       r.spread_bps,
       run.run_id,
       EXTRACT(EPOCH FROM (r.hr - run.started_at)) / 3600.0 AS regime_age_h,
       run.starts_after_gap                  AS age_left_censored,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.mae_4h_pct  / 100.0 * r.entry_price / r.atr END AS mae4,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.mae_12h_pct / 100.0 * r.entry_price / r.atr END AS mae12,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.mae_24h_pct / 100.0 * r.entry_price / r.atr END AS mae24,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.mae_48h_pct / 100.0 * r.entry_price / r.atr END AS mae48,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.mfe_4h_pct  / 100.0 * r.entry_price / r.atr END AS mfe4,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.mfe_12h_pct / 100.0 * r.entry_price / r.atr END AS mfe12,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.mfe_24h_pct / 100.0 * r.entry_price / r.atr END AS mfe24,
       CASE WHEN r.atr > 0 AND r.entry_price > 0 THEN r.mfe_48h_pct / 100.0 * r.entry_price / r.atr END AS mfe48
FROM ranked r
LEFT JOIN v_kairos_regime_runs run
  ON run.symbol = r.symbol
 AND r.hr >= run.started_at
 AND r.hr <= run.last_seen_at
WHERE r.rn = 1;

COMMENT ON VIEW v_kairos_signal_episodes IS
  'Kairos G2: all light-sweep buy/sell signals since 2026-09-22 de-duplicated to (symbol, direction, hour). tier = best outcome in the hour (qualified > suppressed > gate_fail > unknown[pre-168 not-qualified]). m1..m48 = geometry-free signed drift in ATR units from the first tick; mae/mfe 4..48 = fixed-horizon adverse/favourable excursion in ATR units (migration 170). regime_age_h from v_kairos_regime_runs. worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md §12.';

GRANT SELECT ON v_kairos_signal_episodes TO anon;

-- ═══════════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION:
--   SELECT count(*) FILTER (WHERE mae24 IS NOT NULL), count(*) FROM v_kairos_signal_episodes
--   WHERE hr < now() - interval '27 hours';
--   -- invariant: signed drift inside the excursion band
--   SELECT count(*) FROM nt_signal_sim WHERE mae_24h_pct IS NOT NULL
--     AND (drift_24h_pct < -mae_24h_pct - 1e-6 OR drift_24h_pct > mfe_24h_pct + 1e-6);  -- expect 0
-- ═══════════════════════════════════════════════════════════════════════════════
