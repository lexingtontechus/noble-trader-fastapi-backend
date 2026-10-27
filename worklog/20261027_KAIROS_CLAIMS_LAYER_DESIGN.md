# Kairos claims layer — design & consistency contract (no geometry/calibration rewiring)

**Date:** 2026-10-27
**Type:** Design (pre-implementation scope doc + measured-evidence grounding).
**Grounding:** TSFM Probabilistic Forecast Lab (independent harness, built outside this repo) + same-day repo pull of `noble-trader-fastapi-backend` @ HEAD and `nobletradingapp` @ HEAD.
**Contract:** one claim, one fan, one resolver — additive and shadow-first; nothing in the live geometry, EV, or gate paths changes in Phase A.

---

## 0. Why this exists (the measured motivation)

The lab's rolling-origin verdicts (90 origins, frozen arms, DM-tested, re-measured 2026-10-27 after
the trend/regime covariate fix):

| Lane | Verdict | Numbers |
|---|---|---|
| Price 24h | calibrated but unskilled | CRPS ratio 1.073 vs RW (p 0.103), coverage ≈ nominal |
| Direction | no skill | Brier 0.2686 vs coin 0.2497; covariates *hurt* |
| Vol 6–72h | **the one live lane** | 5/15 cells significant vs Log-HAR: BTCUSD 6h 0.727 (p 0.0001), 24h 0.828 (p 0.0023), USDCHF 6h 0.826, USDJPY 24h/72h 0.959/0.910 |
| GARCH family | no HAR displacement | EGARCH-t best-in-family (0.944, p 0.085); GARCH/GJR worse at long horizons on metals |
| Power control | harness detects skill | ratio 0.323 vs seasonal-naive |

Conclusion: the only measured edge is **distributional** (vol) and **calibration** (honest fans).
Kairos currently publishes the two *least* trustworthy layers to the frontend — point brackets
(entry/TP/SL) and a claimed `p_win` — and none of the distribution that makes those numbers
interpretable. The claims layer closes that gap and simultaneously gives kairos an honest,
outcome-independent measurement track ("kairos = arm 11": pre-registered falsifiable claims scored
by the same pinball/coverage/DM machinery that grades the model arms — no p_win, no winrate, no
PnL required).

## 1. The consistency question, answered

**Does this rewire geometry?** No. The geometry pipeline
(`tiered_atr.DynamicAtrPick` → `_compute_trade_signal` → `barrier.py` π guard → brick display)
is untouched. Claims *register and score* the bracket; they never set levels. The only
geometry-adjacent addition is a read-only translation computed at registration: where the bracket
levels sit inside the registered fan (`tp_q`, `sl_q` — "TP at the fan's q92, SL at q18"). This is a
stamp in the repo's own shadow-stamp convention, not an input to level selection.

**Does this rewire calibration?** No existing loop changes. The three live calibration layers
(EOD bias + Bayesian shrink, `p_win_bracket` reachability, PBM Dirichlet posterior) keep running
with their current flags. Claims add a **parallel, outcome-independent scoring surface**
(claim coverage / PIT / CRPS / claim Brier). In a later phase, *if* claim coverage proves reliable
on era-clean replay, it may *inform* the shrink prior via a feature flag (`claims_prior_mode: off`
by default) — opt-in, evidence-gated, same discipline as `pbm_*_mode`.

**Where rewiring pressure actually exists — and the contract that absorbs it.** The real risk is
not gate rewiring, it is *semantic drift between backend and frontend* (units, settlement, grid).
One contract, defined once:

| Concern | Contract | Rationale |
|---|---|---|
| Units at the boundary | **PRICE space** (same units as `entry_price/stop_loss/take_profit`). Log-space stays internal to the forecast arm; convert once at registration. | The lab's own fan-axis bug was a log/price display mismatch. The frontend must never see log values. |
| Settlement semantics | Reuse `settlement_contract.py` (SL-first, intrabar, 48-bar) as the **single** claim resolver. No second resolver. | Claim outcomes must be identical to signal outcomes or the two ledgers diverge silently. |
| Quantile grid | `q05..q95` at the 7 lab levels, hourly steps to `min(horizon, 48h)`. | Matches the `ForecastPoint` precedent already in `nobletradingapp/lib/tsfm-lab/design.ts` — zero new frontend typing. |
| Fan provenance | `source` field: `tsfm_sidecar` \| `vol_backtest_recalib` \| `manual`. Frozen at registration (claims are immutable once written). | Claims are evidence, not live state. |
| Transport | Claim fields ride the **existing** `signal` broadcast event (frontend absorbs name drift via `??` fallback getters) + `v_talaria_latest_signals` extension + `/api/dashboard-data` bundle. **MT5 consumers excluded** — `talaria-copy` `SIGNAL_COLUMNS` and the EA/bridge keep the minimal entry/sl/tp contract. | The bridge/EA copy path must not gain a distribution payload it cannot render. |

## 2. The frontend disconnect, precisely

Current signal-path rendering (all point values): `FloatingSignalsWidget` (ENTRY/SL/TP copy
buttons, `p_win` chip, EV, kelly, RANK, STATE), `RenkoSection` (three horizontal level lines),
`KellyTable` / `SignalRankingBoard` (claimed win% columns). Calibration honesty exists but lives
on *separate* tabs (`CalibrationScorecard`, `CalibrationConfidenceBoard`, Edge Lab reliability
diagram). Meanwhile `deadlineTiers`, `geometryRisk`, `kairosPivots` are already loaded in the
dashboard bundle and rendered nowhere, and the planned `SignalContextCard`
(`docs/worklog-kairos-frontend-harmonization.md` §4.1) was never built.

So the user sees a bracket and a claimed probability, with the honest distribution one screen away
at best. The disconnect is **not** that geometry is wrong — it is that the bracket is presented
*outside* its distribution, and the confidence is presented *without* its calibration. The claims
layer joins them: the fan renders on the signal card with ENTRY/SL/TP drawn *on* it, plus the
already-computed-but-unrendered `p_resolve` / barrier probabilities as the bracket's own
probability read.

## 3. Schema sketch

New table `nt_signal_claims` (sibling of `nt_signal_sim`, keyed by `signal_id`):

```
claim_id, signal_id, symbol, ts, deadline_ts,
kind            -- "bracket" | "vol_fan" | "thesis"
source          -- "tsfm_sidecar" | "vol_backtest_recalib" | "manual"
space           -- "price" (always, at the boundary)
horizon_h,
fan             -- jsonb: [{h, t, q05..q95}] (7 levels, price space)
bracket_ref     -- {entry, sl, tp, tp_q, sl_q}   (fan-space positions of the live bracket)
claimed_probs   -- {p_resolve, p_win_given_resolve, pi_win, pi_loss}  (already computed today)
status          -- "open" | "resolved" | "void"
outcome         -- filled by settlement contract: {result, coverage50/80/90, crps, pinball, brier}
resolved_at
```

Frontend contract additions (lockstep consumers, per the 0029 matview pattern):
1. `v_talaria_latest_signals` gains the latest open claim join (or sibling `v_talaria_signal_claims`).
2. `/api/dashboard-data`: both `loadCachedBundle` and `loadLiveBundle` + `talaria_dashboard_cache`
   payload (refreshed by backend pg_cron).
3. Broadcast payload: optional `claim` object on the `signal` event (fail-open, omitted when None —
   same convention as `p_clef_wait`).
4. New `SignalContextCard`: fan SVG (reuse the `components/tsfm-lab/fan-chart.tsx` precedent) +
   bracket lines + claim chips ("TP at q92 · SL at q18 · fan 80% cov 0.79").

## 4. Phases (shadow-first, flag-gated, nothing live on a calendar)

| Phase | Lands | Flag default |
|---|---|---|
| **A** | `nt_signal_claims` + registration at qualification time (bracket claim mandatory; vol_fan claim when the sidecar/vol arm is available) + EOD resolver scores claims (coverage/CRPS/Brier) via the settlement contract | `claims_mode: "shadow"` — writes only |
| **B** | Transport: matview extension + dashboard-data + broadcast fields | `claims_publish: "off"` |
| **C** | Frontend `SignalContextCard` + widget chips | (frontend, no flag) |
| **D** | vol_backtest GARCH-family port: GJR-t / EGARCH-t arms via `arch` (`vol="GARCH", o=1` / `vol="EGARCH"`, `dist="t"`) alongside the existing GARCH11-N; recalib.py MZ+k spread calibration applies unchanged; lab-measured findings as priors (EGARCH best-in-family; no HAR displacement) | research-only, as today |
| **E** | Claims-vs-arms leaderboard ("arm 11"): kairos claim CRPS/coverage vs the lab arm table on the same symbols/horizons; optional `claims_prior_mode` feeding calibration enforcement | `claims_prior_mode: "off"` |

**Non-goals (Phase A explicitly):** no gate changes, no EV input changes, no geometry/ATR changes,
no MT5 payload changes, no p_win semantics changes.

## 5. GARCH status correction (repo fact-check)

The lab session's GARCH family (GARCH(1,1)-N, GJR-TGARCH-t, EGARCH-t; Nelder–Mead MLE;
unit-variance rescaling; asymmetric variants) is **not** in this repo. What exists:
`vol_backtest/arms.py::GARCH11` — vanilla GARCH(1,1)-normal via the optional `arch` package
(commented in `requirements.txt`; arm reports `available=False` unless installed), research-only,
never in the live path. Phase D above is the port; `arch` supports both asymmetric variants
natively, and `recalib.py`'s MZ+k spread calibration already handles the normal-approx spread
defect it was built to repair (measured coverage80 0.19–0.44 vs nominal 0.80 pre-recalib).

## 6. Success criteria

- A qualified signal in the dashboard renders *with* its registered fan and bracket-in-fan
  positions; MT5 copy path byte-identical to today.
- Every claim resolves through the same settlement contract as its signal; no resolver fork.
- Claim coverage for the vol-fan claims lands in the [0.80, 0.95] Wilson band after Phase D
  recalibration (the recalib.py acceptance bar).
- The arm-11 leaderboard answers, with DM p-values: does the thesis layer's registered
  distribution beat the free TSFM vol fan (lab BTCUSD 6h ratio 0.727) on the same cells?
  If not, the thesis layer is decoration on top of vol targeting — measured, not argued.
