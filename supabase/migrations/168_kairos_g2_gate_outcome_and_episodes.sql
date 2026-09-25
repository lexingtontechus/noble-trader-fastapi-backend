-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 168: Kairos G2 — gate outcome stamps + all-signal episode view
--
-- worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md §12 (weekly G2 roadmap, user
-- 2026-09-24: "I want to be able to ascertain all signals (not just
-- qualified=true due to the cooldown mechanism)"). Additive only: three
-- nullable columns on nt_signal_sim + one VIEW. Stamps are written after the
-- qualification verdict is final and are never read back by it.
--
--   gate_outcome    — 'qualified'  : published (all gates + cooldown + dedup)
--                     'suppressed' : passed EVERY gate, blocked afterwards by
--                                    cooldown / content dedup / a live shadow
--                                    gate (bracket, geometry)
--                     'gate_fail'  : failed ≥ 1 qualification gate
--                     NULL         : row captured before this migration
--                                    (pre-168 rows only know qualified t/f)
--   suppress_reason — cooldown_open | cooldown_time | content_dedup |
--                     bracket_gate | geometry_gate (only for 'suppressed')
--   failed_gates    — comma list of failed gate names (only for 'gate_fail')
--
-- v_kairos_signal_episodes — one row per (symbol, direction, hour) from
-- sweep-light, the de-duplicated unit every Kairos statistic uses (skill
-- rule: ~4 ticks/hour are one decision, not four). Tier = best outcome in the
-- hour (qualified > suppressed > gate_fail > unknown). Edge m_h = signed
-- drift in ATR units (drift_pct/100 × entry / atr) from the hour's first
-- tick — geometry-free. Regime age = hour − run start from
-- v_kairos_regime_runs (left_censored when the run starts after a data gap).
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS gate_outcome text,
  ADD COLUMN IF NOT EXISTS suppress_reason text,
  ADD COLUMN IF NOT EXISTS failed_gates text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'nt_signal_sim_gate_outcome_chk'
  ) THEN
    ALTER TABLE nt_signal_sim
      ADD CONSTRAINT nt_signal_sim_gate_outcome_chk
      CHECK (gate_outcome IS NULL OR gate_outcome IN ('qualified', 'suppressed', 'gate_fail'));
  END IF;
END $$;

COMMENT ON COLUMN nt_signal_sim.gate_outcome IS
  'Kairos G2: qualified | suppressed (passed every gate, blocked by cooldown/dedup/live shadow gate) | gate_fail. NULL before migration 168.';
COMMENT ON COLUMN nt_signal_sim.suppress_reason IS
  'Kairos G2: why a gate-passing signal was not published: cooldown_open | cooldown_time | content_dedup | bracket_gate | geometry_gate.';
COMMENT ON COLUMN nt_signal_sim.failed_gates IS
  'Kairos G2: comma-separated names of failed qualification gates (gate_fail rows).';

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
       run.starts_after_gap                  AS age_left_censored
FROM ranked r
LEFT JOIN v_kairos_regime_runs run
  ON run.symbol = r.symbol
 AND r.hr >= run.started_at
 AND r.hr <= run.last_seen_at
WHERE r.rn = 1;

COMMENT ON VIEW v_kairos_signal_episodes IS
  'Kairos G2: all light-sweep buy/sell signals since 2026-09-22 de-duplicated to (symbol, direction, hour). tier = best outcome in the hour (qualified > suppressed > gate_fail > unknown[pre-168 not-qualified]). m1..m48 = geometry-free signed drift in ATR units from the first tick. regime_age_h from v_kairos_regime_runs. worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md §12.';

GRANT SELECT ON v_kairos_signal_episodes TO anon;

-- ═══════════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION:
--   SELECT tier, count(*) FROM v_kairos_signal_episodes GROUP BY 1;
--   SELECT gate_outcome, suppress_reason, count(*) FROM nt_signal_sim
--   WHERE ts > now() - interval '1 day' GROUP BY 1, 2;
-- ═══════════════════════════════════════════════════════════════════════════════
