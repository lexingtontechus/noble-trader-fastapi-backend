# Pulse check — 2026-09-30 23:10 UTC

**Deploy:** Studio = local `65b1d52` (in sync at check). **Follow-up commit `81840de`**
(fixes UnboundLocalError — **not yet on Studio**; pull needed).

---

## 0. Roadmap — overdue first (updated in place)

| mechanism | was due | action |
|---|---|---|
| `kairos_daily_job` | 09-29 | **OK** heartbeat 22:20Z errors=0 → review +7d |
| `eod_resolver_scan_full` | 09-29 | **OK** full scan + `expired_missing_exit_price=0` → +7d |
| `calibration_evidence_pool` | 09-29 | shadow `[eod:pool]` 14/14 symbols; q+s often thin → **HOLD** +5d |
| `p_win_in_ev_mode` | 09-30 | live expectancy n=2 → **HOLD** +7d |
| `light_publish_geometry_gate` | 09-30 | still shadow; B5 bar not met → **HOLD** +7d |
| `publish_path_redesign` | 09-29 | cooldown_time working (no cooldown_open) → +7d |
| `tier1_context_alignment_gate_scope` | 09-27 | 48h false=21/1000 (2.1%) → still no live case → +10d |
| `aapl_regime_staleness` | 09-27 | **blocked** (trend_edge rows exist but stale computed_at 09-17/25) |

**New rows inserted:** `atr_monthly_reopt`, `atr_reopt_urgency`, `dynamic_atr_vol_ratio_gate`.

---

## 1. Deployment drift

| | |
|---|---|
| Local @ check | `65b1d52` vol-urgency Discord nudge |
| Studio | `65b1d52` — **match** |
| Local now | `81840de` UnboundLocalError fix — **needs Studio pull** |

---

## 2. Studio logs

- Span: 2026-09-29 11:01 → 2026-09-30 23:05 UTC (truncated file)
- `Starting sweep orchestrator`: 28 · **Tracebacks: 0** · ERROR: **10**
- Errors: `record_signal_stream_meta_shadow` ×7 · `record_heavy_sweep_atr_anchored_shadow` ×1 · `log_renko_bricks` ×1 · `eod_resolver PATCH` ×1

## 3. Scheduler catch-up

Stale catch-ups fired as designed: calibration state, macro regime, two light-sweep slots (18:15, 22:15). Jobs registered include **Monthly ATR free-lattice re-opt** + **ATR re-opt vol urgency nudge (4h)**.

## 4. EOD / pipeline

| metric | value |
|---|---|
| last resolved | 2026-09-30 22:38 UTC (fresh) |
| resolved 24h | **1353** — tp 403 / sl 636 / **expired 314 (23.2%)** |
| expired_missing_exit_price | **0** (regression check PASS) |
| eod_actions 24h | geometry_tuning_sweep 193 · emergency_heavy_sweep 9 · kelly_reduction 9 · bayesian_penalty 3 · regime_shift_alert 1 |

## 5. Calibration bias (enforced / raw)

OVERCONFIDENT enforced: AAPL, AUDUSD, BTCUSD, EURUSD, GBPUSD, US500, XAUAUD, XAUEUR, XAUUSD. UNDERCONFIDENT: US30USD (−0.37). CALIBRATED: USDCAD, USDCHF, USDJPY, XAGUSD.

## 6. Kairos

Hourly `:20` OK through 22:20Z — drift_backfill errors=0, rank_ingest 500, calendar fresh.

## 7. Heavy sweep (17:06 UTC)

**14 ok / 0 failed.** Dynamic ATR after new by_symbol grids:

| outcome | symbols |
|---|---|
| no qualifying TP/SL (heavy kept) | AAPL, USDCAD, US500, USDCHF, USDJPY, XAUAUD, XAGUSD, XAUEUR, **XAUUSD** (grid 4.94/1.00 loaded) |
| **UnboundLocalError** → heavy kept | **GBPUSD, US30USD** (qualifying pick existed) |
| vol-urgency Discord 20:00Z | **BTCUSD** (1.36), **USDCAD** (1.30) |

Tight clamps + 10d walk often yield no pick → fail-closed. That is correct but
means researched cells only apply when the 10d walk fits inside the box.

## 8. Bug fixed this pulse

`heavy_sweep.py` logged `to_price_levels(trade_signal…)` **before** `trade_signal`
was built → UnboundLocalError → `_dyn_live` never applied. **`81840de`** logs
ATR multiples only; PRICE apply path unchanged. **Studio must pull.**

---

## Dynamic ATR review (summary)

- XAUUSD **4.94/1.00** is in the live grid (seen in log).
- BTCUSD **8.07/1.77** + `vol_ratio_max=0.71` registered; urgency nudge fired.
- Most symbols fail closed to heavy today — expected with narrow clamps.
- Next: deploy `81840de`, then confirm PRICE brackets appear for a successful pick.
