# Kairos claims layer — Phase D build (the recalibrated vol fan)

**Date:** 2026-10-28
**Type:** Implementation (Phase D of `worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md` §4).
**Scope guard:** additive, flag-gated (`claims_vol_fan_mode: off` — the design doc's
"research-only, as today"), AND-gated on `claims_mode`. Zero changes to geometry,
EV inputs, gates, p_win semantics, MT5 payloads, calibration loops, or the
claims_resolver (its kernels were already kind-agnostic — a vol_fan row with the
Phase A fan schema scores with **zero resolver changes**, verified below).

---

## 0. What landed

| Piece | File | What it does |
|---|---|---|
| Vol-fan module | `sweep_orchestrator/claims_vol_fan.py` (new) | The GARCH-family price fan + MZ+k multiplier + mtime-cached artifact loader + `register_vol_fan_claim` (Phase A write pattern + Phase B publish pattern, fail-open everywhere). |
| Public arm seam | `vol_backtest/arms.py` | `variance_path(prices, horizon)` — thin public wrapper over `_fit_forecast_variance` (the per-horizon path `forecast()` collapses; verified identical output). vol_backtest stays outside regime_platform except for this deliberate bridge. |
| Flags | `feature_flags.py` + `feature_flags.yaml` | `claims_vol_fan_mode: off` (off\|shadow), `claims_vol_fan_arm: egarch` (garch\|gjr\|egarch — the PRE-REGISTRATION), `claims_vol_fan_calib_path` (repo-relative artifact, mtime hot-reload). Full triples: _DEFAULTS + getters + commented yaml. |
| Wiring | `orchestrator.py` + `heavy_sweep.py` | Second fire-and-forget task inside `if qualified:` (both paths, one definition shape): heavy passes its in-scope 2160-bar window (the exact prices the signal was computed on); light passes its ~200-bar window which the task knows is below the arm floor and refetches 2160 bars itself — the sweep never waits. |
| Migration | `supabase/migrations/220_claims_vol_fan.sql` | `nt_signal_claims` + `arm`/`calib_level` (nullable, commented) + matview rebuild (183/219 DROP-CASCADE precedent): LATERAL gains `c.arm, c.calib_level` and the **kind-priority tiebreak** `ORDER BY c.ts DESC, (c.kind='vol_fan') DESC`. |
| Calibration artifact | `config/claims_vol_fan_recalib.json` | 60 cells (5 symbols × H∈{6,12,24,48} × {garch,gjr,egarch}), all `cell`-level, lockbox-scored — minted today (§2). |
| Research runs | `worklog/kairos/reports/vol_backtest/20261028_phaseD_run_{btc,a,b}/` + `20261028_phaseD_recalib/` | 12,621 records; `records.csv` committed so the recalib is reproducible from the repo alone. |

## 1. Design decisions (and why)

**The fan is the arm's own conditional-variance path, in price space, MZ+k
recalibrated — one correction mechanism, never stacked.**
`var_path[h-1]` from the pre-registered arm → cumulative
`σ_raw(h) = √(Σ_{i≤h} var_i)` → per-horizon multiplier
`m(h) = k·exp(a)·σ_raw(h)^(b−1)` (= `k·F'/F`, the recalib.py MZ center `F'=exp(a)F^b`
composed with the spread inflation) → `q = entry·exp(z_q·σ_raw(h)·m(h))` on the same
7 lab levels / hourly h=1..48 grid / 6dp rounding as Phase A — so claims_resolver
scores it unchanged. **The k transfer is a stated modeling choice, not a measured
fact**: recalib fitted k against the arm's claimed *RV-scale* interval; the price
fan's log-spread at horizon h *is* σ(h), and the same normal-approx understatement
(t-tails + parameter uncertainty, horizon-growing) hits both. The claims ledger is
the instrument that measures whether the transfer worked — coverage50/80/90 + CRPS
of vol_fan claims vs realized closes is exactly design §6's acceptance criterion.
Deliberately NOT t-quantiles AND k (double correction), NOT simulation quantiles
(noise at q05/q95 with 300 sims) — normal z + one measured multiplier.

**Per-horizon interpolation, identity where there is no evidence.** Cells exist
only at the backtest horizons {6,12,24,48}: exact cell at those h; log m linear in
h between bracketing cells (level = the WEAKER bracketing cell); **identity below
6h** (no evidence under 6h — never extrapolated downward); clamp-to-nearest above.
`calib_level` on the row = the terminal-horizon (h=48) cell's level — the horizon
coverage90 cares most about; a missing/corrupt artifact ⇒ identity, stamped
`'none'`, never an error, never a blocked claim.

**Arm pre-registration via flag.** `claims_vol_fan_arm: egarch` default = the lab's
measured prior (EGARCH-t best-in-family, 0.944 vs Log-HAR; GARCH/GJR worse at long
horizons on metals). Setting the flag IS the pre-registration; the row's `arm`
column (migration 220) makes any switch auditable evidence, never silent
cherry-picking.

**Windows.** Heavy: the sweep's own 2160 bars (exact signal window, zero extra
fetch). Light: the task fetches its own 2160-bar window via the sweep's own
fetcher (`include_timestamps=True`) — a fire-and-forget fetch of ~1 request per
qualified signal (rare by measurement: 99.6% stand_by). Calendar filter applied
when timestamps are present (the recalib cells were fitted on calendar-filtered
series — same convention); a window that would filter below the 720-bar arm floor
fits unfiltered (real TDVA FX feeds are already calendar-clean; the fallback only
guards degenerate synthetic windows). The fit runs in `asyncio.to_thread` — the
event loop never blocks on a 0.02–0.2s MLE.

**Kind priority in the matview (the 219 tie, resolved).** A signal's bracket and
vol_fan claims share the same `ts`, so 219's `ORDER BY c.ts DESC` was a
nondeterministic tie once vol_fan rows exist. 220 adds `(c.kind='vol_fan') DESC`:
latest claim per symbol, and within one signal the vol_fan fan is the display fan
(the design §2 end state — ENTRY/SL/TP drawn on the honest distribution), while the
bracket claim stays in the ledger as the `atr_normal` control arm it was registered
as. While `claims_vol_fan_mode=off` no vol_fan rows exist and the view is
byte-equivalent to 219; a failed vol_fan registration degrades the view to the
bracket claim automatically (fail-open by construction).

**Degenerate-fit guards (found by verification, not by review).** A blown EGARCH
fit (β≈1 on an ARCH-free window) stays "finite" while its simulated variance path
explodes — `exp(z·σ)` then overflows into garbage quantiles. The arms' research
aggregate only checks finiteness/positivity. The fan builder now rejects
(as None — fail-open no claim): any per-bar σ > 0.5, terminal cumulative σ > 10,
or post-multiplier scale > 15. Absurd-value thresholds, not tuning; the caught
case is in the verification record.

**Determinism.** GARCH/GJR analytic forecasts are deterministic; EGARCH's is the
mean of seeded simulations (seed=7) — same window ⇒ same fan, verified. Claim
immutability holds.

## 2. The research run that minted the cells (honest provenance)

TDVA is unreachable from this sandbox (no RapidAPI creds — the same reason the
sweep cannot run here), so the runs used the **TSFM lab's real broker hourly CSVs**
(`data/tsfm/*.csv`, 2024-10-21 → 2026-10-20, the exact vintage behind the lab's
measured truths: BTCUSD 6h 0.727 / 24h 0.828 etc.) staged into
`data/vol_backtest/` — the runner's documented real-data path #1. The operator can
re-mint from TDVA (the live feed) with the same commands + creds; the artifact's
provenance rides in each run dir's `run_config.json` (`source: csv:data/vol_backtest`).

```
python scripts/run_vol_backtest.py --data-dir data/vol_backtest --symbols BTCUSD \
    --arms garch,gjr,egarch --horizons 6,12,24,48 --spacing 72 --min-train 720 \
    --out worklog/kairos/reports/vol_backtest/20261028_phaseD_run_btc
# (…same for run_a = XAUUSD,XAGUSD and run_b = USDJPY,USDCHF)
python -m vol_backtest.recalib \
    --run-dir worklog/kairos/reports/vol_backtest/20261028_phaseD_run_btc \
    --run-dir worklog/kairos/reports/vol_backtest/20261028_phaseD_run_a \
    --run-dir worklog/kairos/reports/vol_backtest/20261028_phaseD_run_b \
    --out worklog/kairos/reports/vol_backtest/20261028_phaseD_recalib
cp worklog/kairos/reports/vol_backtest/20261028_phaseD_recalib/recalib.json \
   config/claims_vol_fan_recalib.json
```

Spacing 72 (3-day cutoffs) keeps target-window overlap ≤ 2/3 while n_train
(121–164 per cell) clears `min_n_cell=60` — every one of the 60 cells is
`cell`-level evidence. Fit cost: 12,621 forecasts in ~17 min wall (2 processes).

**Lockbox verdict (the artifact's own honesty):** 24/60 cells Wilson-ACCEPTED;
the headline defect is exactly what recalib was built for — raw coverage80
**0.03–0.55** (stated uncertainty 2–20× too narrow; BTCUSD egarch H48 raw 0.03),
calibrated **0.62–0.87**, e.g. BTCUSD egarch H48 0.03→0.85 ACCEPT,
USDCHF egarch H12 0.38→0.85 ACCEPT, k factors 1.5–3.5 (egarch) and up to 5.5
(garch on USDCHF). The non-accepted cells are small-n point estimates (read the
CIs: 0.67 with [0.55, 0.77] is sampling noise at n_test≈68, per the report's own
reading notes) — and the acceptance bar that matters for the CLAIMS layer is
measured live: the ledger's coverage on scored vol_fan claims over the coming
weeks (design §6), which is precisely what the claims layer exists to measure.

## 3. Verification (all offline; no test files added, per the Phase A/B discipline)

* **Inline suite: 47/47** (`/home/z/.phaseD/verify_phase_d.py`, stubbed httpx +
  forced settings, house patterns): multiplier math (exact cell k-doubling, MZ
  center b=0.5 ⇒ m=5.0, log-linear interpolation √8 exact, identity below min
  cell / wrong symbol, weaker-bracket level, both clamp bounds); artifact loader
  (60 cells, missing ⇒ {}, corrupt ⇒ {}); fan construction on real GARCH fits
  (48 rows, 7 levels, monotone in q, log-symmetric with median=entry exactly,
  √h scaling on iid window w48/w6=2.685 vs √8=2.828, <720 bars refused,
  `variance_path` ≡ `_fit_forecast_variance`, EGARCH deterministic on a clustered
  window, blown-fit window rejected); calendar filter (drops Sat+Sun-pre-22:00,
  keeps crypto, below-floor fallback); **Monte-Carlo 400×48: coverage
  0.498/0.795/0.897 vs nominal 0.50/0.80/0.90** (fan math + scorer end-to-end,
  Phase A's strongest check, repeated for the GARCH fan); offline round-trips
  (flag off ⇒ no POST; on ⇒ exactly one POST with kind/source/arm/calib_level,
  48-row fan, deadline=+48h, tp_q/sl_q in (0,1), claimed_probs, column whitelist;
  **real artifact cell applied: cal/raw = 2.117 = k·exp(a)·F^(b−1) to 3 decimals**
  with k=3.53, a=−1.047, b=0.798; publish off ⇒ no broadcast, live ⇒ one broadcast
  carrying the vol_fan claim; arch missing ⇒ fail-open; short window ⇒ fetch path;
  fetch failure / HTTP 500 ⇒ no claim, no raise); resolver scores a vol_fan row
  generically (take_profit echo + coverage/crps/brier at n_scored_h=48); migration
  220 sqlglot-parsed — the only failures are the same COMMENT ON/notify parser
  gaps that fail identically on operator-applied 218/219.
* **Real-data ship demo:** EGARCH on the last 2160 real BTCUSD bars + the real
  artifact: σ_raw(48)=0.0388, m=1.925, `calib_level='cell'`, 48h fan
  72,507…92,680 around entry 81,976 (±13%), a 3%/6% buy bracket stamps
  TP q93 / SL q22 — meaningful positions in the honest distribution.
* **Ruff:** new file 0 errors (after import-sort + line-length fixes); the four
  edited files = exact HEAD baseline (53=53); repo-wide 798=798 net-zero.
* **py_compile** all 5 touched files; yaml parses.
* **Existing suites:** Phase A/B set (`eod_resolver, eod_lookback_wiring,
  signal_sim_capture, settlement_contract, eod_resolver_basis, pbm_heavy_guard,
  barrier_null`) **129/129**; broader `-k "flag or wiring or eod or signal_sim or
  claims or vol_backtest or recalib"` sweep **298 passed, 1 failed** —
  `test_e2e_registry_wiring` calibration-router threshold, **verified pre-existing
  at HEAD via stash round-trip**; 2 collection errors (missing optional deps,
  pre-existing).

## 4. What the operator must do (in order)

1. **Apply `supabase/migrations/220_claims_vol_fan.sql`** in Supabase (SQL editor
   / management API), like 210–219. Requires 218+219 (applied). Additive only:
   two nullable columns + the matview rebuild; while the flag is off nothing
   observable changes.
2. **Install `arch` on the deployment** (`pip install "arch>=7.0.0"` — the
   requirements.txt line is deliberately still commented; the dependency stays
   optional and the claim degrades to a silent no-op without it, but the flag
   cannot fire without it).
3. Nothing else is required. `claims_vol_fan_mode` ships **off**. When you want
   the vol fan live in shadow: flip to `"shadow"` (mtime hot-reload, next
   qualified signal). Optional: re-mint `config/claims_vol_fan_recalib.json`
   from TDVA with §2's commands for feed-matched cells.

## 5. Honest limitations (measured, not argued)

* **The k-transfer is a hypothesis the ledger will test**, not a measured fact:
  k was fitted on RV-scale intervals and is applied to the price fan's log-spread.
  If the transfer is wrong, vol_fan claim coverage will say so — that is the
  point of the claims layer (the §6 Wilson band is a live measurement, weeks of
  clock time, not a today-verification).
* **The artifact's feed is the lab's broker CSVs, not TDVA** (sandbox has no
  RapidAPI creds). Hourly closes are hourly closes to first order, and the run
  dirs commit every (cutoff, forecast, actual) pair — but feed-matching to the
  live path is the operator's re-mint away.
* **Cells cover the 5 thesis symbols.** Qualified signals on other symbols
  register vol_fan claims with identity calibration, stamped
  `calib_level='none'` — honest, visible in the ledger, and the coverage numbers
  will show it.
* **The fan asserts zero drift** (median = entry at every h, like Phase A) —
  σ comes from the GARCH forecast, the center is not a return forecast. On
  persistent trends the fan will be one-sided-wrong exactly as a vol fan should
  be; the CRPS/coverage scoring will measure that too.
* **Light-path fans anchor on a fresh fetch** (milliseconds after qualification,
  possibly one bar newer than the signal's window); heavy-path fans use the exact
  signal window. Heavy-path EGARCH also fits unfiltered (no timestamps in scope)
  while the recalib cells were fitted on calendar-filtered series — TDVA hourly
  feeds are already calendar-clean, so this is a documentation note, not a
  measured divergence.
* **Rejected lockbox cells still ship their (a,b,k)** — the artifact applies
  every cell-level fit regardless of the research acceptance verdict; the
  acceptance bar that governs is the live claims ledger's, and the artifact's
  `cov_oos*` fields are carried for Phase E slicing.
* `horizon_h` stays fixed 48h (the settlement window), same as Phase A.

## 6. Phase E hooks left ready (not built)

The claims ledger now holds sibling bracket/vol_fan claims per signal with
`arm`/`calib_level` stamps and the recalib artifact's lockbox diagnostics — the
substrate for the "kairos = arm 11" leaderboard (claim CRPS/coverage vs the lab
arm table on the same cells, DM-tested) and the optional
`claims_prior_mode` (feeding calibration enforcement), each gated on operator
green-light per the design doc §4.
