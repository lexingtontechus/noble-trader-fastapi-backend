# worklog/kairos — Kairos reports

Kairos v3 (geometry-free edge-state research) output. Spec:
`worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md` (§11 P1 results, §12 weekly G2 roadmap).
**Binding cut-list / quant decisions (2026-09-26):** `20260926_METHODOLOGY_TRIAGE_AND_QUANT_DECISIONS.md` and scope §15 —
primary test is G2.2; L4/L5/L7 + HSMM + factor families 6–10 archived; worktree retired onto `main`.

| Path | What |
|---|---|
| `scopes/` | Kairos scopes: edge-state (09-23), regime-cell persistence (09-23), V3 (09-24) |
| `reports/YYYYMMDD_{Wn,A<mmddHH>}.{md,json}` | G2 looks — `scripts/kairos_weekly_check.py` (weekly, snapped to Thu 08:00) or `--accel` (rolling 48h, daily). `20260924_W0_partial` is the run first mislabelled W1 |
| `20260925_RESTART_SAFE_STATE_AND_ACCELERATED_G2.md` | Restart-safe regime state, hourly drift backfill + heartbeat, accelerated G2 |
| `20260925_PATH_RISK_AND_REGIME_SURVIVAL.md` | Fixed-horizon MAE/MFE (migration 170, report §E2), Kaplan-Meier survival + remaining regime clock (§E), parked S2/TimesFM-3 covariate plan |
| `20260926_HOURLY_CALIBRATION_RESOLVE.md` | V3 §14.2 lever 1: hourly (`:38`) calibration resolve, `calibration_resolve_hourly_mode` shadow → live. Nightly-only resolver delays evidence ~11h (S2 Brier −0.025 nightly vs −0.043 hourly); shared resolver lock; promotion bar |
| `20260926_CALIBRATION_EVIDENCE_POOL.md` | V3 §14.3: bias from qualified+suppressed evidence only (`calibration_evidence_pool_mode` shadow). View `v_calibration_records_tiered` (migration 171, join on `signal_id`, not a new column / `nt_sweep_result`); AAPL flips CALIBRATED→OVERCONFIDENT; pool thin (2/14) until geometry tightens |
| `20260926_RESOLVER_HEAD_OF_LINE_FIX.md` | Resolver skipped decided TP/SL behind ~1,000 still-young pending signals (nightly wrote 21 SL while 83 were decided; losses under-written → bias skewed to wins). `eod_resolver_scan_mode: full` scans the whole queue; live on deploy |
| `20260926_METHODOLOGY_TRIAGE_AND_QUANT_DECISIONS.md` | Methodology triage + binding quant decisions: G2.2 primary, p_win discrimination or replacement, promote order (pool → hourly), era splits, ESS/power, archive list, worktree → main |
| `20260926_LIVE_G22_DECISION_AND_PROMOTION.md` | Live read: G2.2 **NO PASS at power** (Δm₂₄ −0.32); overconfident p_win every era; pool/hourly **HOLD** (post-flip S2 n=0); 3h HL not promoted |
| `reports/20260926_postflip_rescore.md` | Era-split scorecard + evidence-pool Brier proxy + G2.2 power (script `scripts/kairos_postflip_rescore.py`) |
| `20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md` | Event-time regime transitions (fresh vs deep **T1 supported** +0.117), type split (`dir_flip` best), p_win ranking **fails** (IC≈0); build `kairos_rank.py` |
| `20260927_P0_P4_PROGRAMME.md` | Beyond-p_win programme: **P0 done** (dir_flip h≈6h), P1 TimesFM dynamic covariates (2.5→3.0), P2 typed pivots, P3 R-Layer ranker, P4 macro/LLM extract |
| `20260928_P2_PIVOTS_COMPLETE.md` | P2 done: pivots on **`nt_macro_forecast`** (migration 181), calendar stays raw TDVA cache; lead-lag = co-move 1.3×@0–6h, not multi-day lead |
| `20260928_MACRO_FORECAST_REFACTOR.md` | **Refactor 182:** uniform multi-asset `nt_macro_forecast` (thesis/drivers/risks + structure_* + events); LLM deconstructs narrative; next = apply migrations, fill universe, P3 |
| `20260928_TIMESFM_QUANTILE_CAPTURE.md` | **P1.3:** capture discarded TimesFM `quantile_forecast` (q10/q50/q90 + spread/pos) into `nt_signal_sim` (migration 186). Research stamps only — no gates/Kelly/EV |
| `20260930_VOL_REGIME_ATR_BTC_XAU.md` | **`vol_ratio` ≠ volume.** 45d vol-tercile ATR solve: XAUUSD **4.94/1.00** pooled; BTCUSD **8.07/1.77** only if `vol_ratio≤0.71`. Crypto trend-runner (far TP, wr≈0.25) is a separate product. Scripts `vol_atr_solver.py` / `vol_atr_diag.py` |
| `20260930_MONTHLY_ATR_REOPT.md` | Rediscovery clock: APScheduler monthly free lattice (proposal-only). Not Studio cron; heavy sweep stays daily local clamps. Root `test*.py` → `scripts/legacy_tests/` |
| `scopes/20260927_P0_HALFLIFE_T2_SCOPE.md` | P0 half-life scope + T2 result |
| `scopes/20260928_RANK_BOARD_BROADCAST_DISCORD.md` | **Rank board** → nobletradingapp Realtime + Discord: RANK vs p_win vs EV for capital priority |
| `reports/20260927_halflife_t2.md` | Live T2 event-time half-life report |
| `reports/20260926_rank_transition.md` | Live R1 ranking IC + transition event study |
| `reports/20260926_rank_score_eval.md` | Walk-forward composite `rank_score` + shadow ΔD gate (episode-deduped) |
| `assets/` | Edge-plane concept + P1 discovery HTML |
| `p1/` | P1 discovery on history: `20260924_p1_discovery_nc{2,1}.txt` (n_confirm 2 primary / 1 sensitivity), summary json, cell×age matrices, html plane, 2026-09-23 smoke run |
| `tests/` | Full-suite pytest logs at P0 (2026-09-23) and G2 labels (2026-09-24) |
| `backfill/` | 2026-09-24 insert-loss backfill: the script, its validation, the stamp-recompute check, and the `returning` rows it wrote |

## Sweep cadence (for reading any gap)

Light sweeps run every **15 min on the hour** (:00/:15/:30/:45). One slot writes 14
rows (one per symbol). A 15-min gap between rows is the normal cadence. The weekly
script measures coverage per **15-min slot** and never from gaps between consecutive rows.

A slot is uncovered only when the orchestrator process was down. On 09-23/24 every
uncovered block matches a Studio `on-start-*.log` (the Studio restarted):
09-23 05:00–06:15 → on-start 06:02; 09-24 05:00–06:15 → 06:01;
09-24 12:30–14:15 → 14:09; the 22:15 slot on both days → 22:17. Restarts that land between
slots (e.g. 10:05, 18:13) cost nothing. The earlier blocks (09-18, 09-21, 09-22) predate
the on-start logs checked, so their cause is unverified.

Other slot gaps that are **not** failures:
- `nt_sweep_result` is missing US30USD/US500/USDCAD/USDCHF at the **17:15** slot every day.
  The daily heavy sweep (`6 17 * * *`) holds those symbols' per-symbol lock, so the light
  sweep logs `locked — skipping (concurrent sweep)`. Their 17:15 `nt_signal_sim` rows come
  from the heavy sweep, which is by design.

## 2026-09-24 insert-loss fix + backfill

**Cause.** `_insert_row` used a 5 s client timeout and never retried. Under light-sweep
write pile-ups Supabase answered with ReadTimeout, 502 or 504. On top of that,
`log_signal_sim` treated *any* failure as a schema lag and retried **without** the
Kairos/G2 columns. For `nt_signal_sim` the first request had usually landed, so the
stripped retry cost nothing. When it hadn't landed, the row was re-inserted without
its labels.

**Fix** (`regime_platform/sweep_orchestrator/supabase_client.py`):
- timeout raised to 15 s;
- connect/pool errors and HTTP 503 are retried once on every table;
- `idempotent=True` tables (`nt_signal_sim`, whose client `signal_id` is the PK) also
  retry ReadTimeout/502/504. A 409 on the retry means the row was already written;
- columns are dropped **only** on HTTP 400 `PGRST204` (unknown column, i.e. Studio
  deployed ahead of a migration);
- `log_regime_entry` also retries 502/503.

Non-idempotent tables such as `nt_eod_actions` and `nt_calibration_records` are not
retried after a timeout, because a duplicate would double-apply.

**Backfill** (`backfill/20260924_backfill.py`, applied 2026-09-24):

| Table | Rows | Source (validated on 3,670 healthy ticks, 100.0% equality per column) |
|---|---|---|
| `nt_signal_sim` | 3 labels (XAGUSD 15:02, EURUSD 18:01, BTCUSD 21:45) | log verdict line. Control: the same derivation matches **710/710** stored labels |
| `nt_sweep_result` | 6 rows (09-23 US30USD 17:01, AUDUSD 19:00, US30USD 19:01; 09-24 AUDUSD 16:00, US500 16:01, BTCUSD 18:45) | same-tick `nt_signal_sim` (decision fields) + `nt_regime_log` (markov/TA/shift) + log `p_timesfm`. `error` = backfill marker |
| `nt_regime_log` | 1 row (AUDUSD 21:45) | same-tick `nt_sweep_result` |

**Not backfilled.** Nothing is estimated; these stay NULL:
- the 5 Kairos factor stamps on the 3 rows. A TDVA re-fetch reproduces only 16/39
  control rows exactly (bar revisions and forming-bar timing), and `spread_bps` was a
  live quote snapshot;
- the backtest KPIs, `kelly_f`, `size_mult`, `prev_regime` and `sweep_duration_ms`
  on the 6 `nt_sweep_result` rows, because no exact same-tick source exists.

Both are excluded from Kairos analysis by construction: the stamp-coverage check
counts them and edge statistics drop NULL factors.

`nt_eod_actions` (09-23 18:01 ReadTimeout) needed nothing: the GBPUSD row landed despite
the client timeout.
