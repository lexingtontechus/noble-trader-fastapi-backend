# Benchmark — "Trading Edge Calibration" vs the measured findings

**Date:** 2026-10-02 · **Type:** review / benchmark (no code change)
**Input:** attached "Trading Edge Calibration" analysis
**Compared against:** `docs/scopes/20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md`
and its four measurements (`calibration_decomp`, `cell_conditioning`,
`reachability_decay`, `common_factor`)

Every claim below was checked against the code, not taken on trust. Where the
attachment and the measurements disagree, the code decides.

---

## 1. Verdict

**The diagnosis is right and independently confirmed. Three of the five "existing
mechanisms" exist and are accurately described. Two do not exist as described, and
the headline recommendation (switch sizing to Masaniello) does not do what it claims —
because Masaniello's quality factor is itself a function of `p_win`.**

The attachment's genuinely valuable contribution is one point my own work missed:
**the MAE/path-dependency gap.** I scored calibration against `tp_hit` from a
close-walk and never conditioned on adverse excursion. That is a real hole in my
measurement, and the columns to close it already exist and are unused.

---

## 2. Claim-by-claim verification

| # | Attachment claim | Verdict | Evidence |
|---|---|---|---|
| — | Binary `P_win` is the wrong calibration object | ✅ **confirmed** | bracket AUC **0.486**; `REL` 0.0289 vs `RES` 0.0011 (overconfident, almost no resolution) |
| — | Payoff asymmetry ignored | ✅ confirmed | RR 1.67–2.0; my Brier work has the same blind spot |
| — | Path dependency / MAE disregarded ("ghost edge") | ✅ **confirmed — and it's a gap in MY work** | `mae_4h/12h/24h/48h_pct` + `mfe_*` exist (migration 170) and are **unused** |
| — | Classifiers emit uncalibrated scores clustering near 0/1 | ✅ confirmed | my reliability table: gap +0.316 at 0.0–0.2, −0.275 at 0.8–1.0 |
| 1 | Kairos v3 `m_h` geometry-free edge exists | ✅ **exists** | `scripts/kairos_discovery.py`: `m_h = d·(C[t+h]−C[t])/ATR_t`, horizons 1/4/12/24/48h, *"No SL/TP, R, W/L or P/L anywhere"* |
| 1b | *"E(C,a) ≤ 0 blocks entries"* | ❌ **not implemented** | `worklog/kairos/20260925_PATH_RISK_AND_REGIME_SURVIVAL.md`: *"Both are **measurements only**. No gate, knob or TimesFM input reads them."* |
| 2 | Vol-tier reachability pre-filter exists | ✅ exists, **shadow** | `heavy_sweep_reachability_mode: shadow` (`feature_flags.yaml:374`) |
| 2b | *"disqualified before execution"* | ❌ **overstated** | shadow = evaluate + log only; it disqualifies nothing live |
| 3 | Dynamic Masaniello exists, formula `f_i = β(0.5+M_i)Q_i·DD_i·V_i` | ✅ **exists, formula exact** | `core/position_sizer.py:18` — verbatim match |
| 3b | *"replaces static P_win-scaled Kelly fractions"* | ❌ **FALSE — see §3** | `Q_i = prob_factor × regime_factor × conf_factor`, and `prob_factor = clip((p_win − min_prob)/0.10, 0, 1.5)`, `min_prob = 0.50` |
| 4 | *"High entropy → reduce Kelly by 50%"* | ❌ **not found** | only `tda_engine.py` persistence entropy (topological, 0d/1d) — a different quantity, feeds nothing |
| 4b | *"≥3 shifts/24h → block entries"* | ❌ **not found** | `_count_shifts_24h` exists (`orchestrator.py:3300`) but is **telemetry**: *"for the heartbeat payload the agent consumes"* |
| 5 | Raw Brier audit on `predicted_p_win_raw` | ✅ confirmed | I used the same column in `calibration_decomp` |
| — | Isotonic / Platt on rolling OOS folds | ✅ correct practice | same as my §4.3 proposal; **not yet built** |
| — | Volatility targeting (inverse vol) | ✅ correct practice | already present as `V_i = clip(ATR_base/ATR_current, 0.5, 1.5)` |
| — | Joint MAE/MFE calibration for SL/TP | ✅ correct practice | columns exist (migration 170), unused |

---

## 3. The load-bearing problem with the headline recommendation

The attachment recommends **"deprecate `P_win`-scaled Kelly in favour of
DynamicMasanielloSizer."** Checked against the code, this does not remove the
dependency — it relabels it.

```
f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i
                    │
                    └── Q_i = prob_factor × regime_factor × conf_factor
                              │
                              └── prob_factor = clip((p_win − min_prob) / 0.10, 0.0, 1.5)
                                                 min_prob = 0.50          ← SizingConfig
```

`prob_factor` is a **monotone function of `p_win`**, and `min_prob = 0.50` is the same
hard floor the quant audit already flags as **E7** (*"`p_win` hard floor 0.50 rejects
positive-EV high-RR low-p trades"*). So adopting Masaniello as-is would:

1. inherit every miscalibration I measured (`REL` ≫ `RES`, bracket AUC 0.486), and
2. **entrench the 0.50 floor** that the audit says discards the best setups.

There is also `use_kelly_overlay=True`, which caps the Masaniello fraction at the
fractional-Kelly bound — i.e. Kelly is *still in the loop* by configuration.

**What the formula does get right, and it is worth saying:** `M_i` (batch urgency),
`DD_i` (drawdown protection) and `V_i` (inverse-vol) are **all `p_win`-free**. `V_i` is
precisely the volatility-target scaling the attachment recommends elsewhere. So the
formula's *scaffolding* is sound; only its *quality input* is the problem.

### The fix both analyses point at

> **Keep `M_i`, `DD_i`, `V_i` — the `p_win`-free risk backbone. Replace `prob_factor`'s
> input with the calibrated reachability probability.**

```
prob_factor ← f( P(tp_hit | symbol, vol_tier, distance/ATR) )     # calibrated, cross-sectional
                instead of raw p_win                               # directional, uncalibrated
```

This is the convergence of the two analyses: the attachment supplies the **sizing
vehicle**, my measurements supply the **probability to feed it**. Neither is sufficient
alone — and the attachment's own recommendation fails precisely at the seam.

---

## 4. Where the attachment improves on my work

**The MAE/ghost-edge point is a real gap in my measurement.** I scored calibration
against `outcome = tp_hit`, which the resolver produces from a **close-walk checking TP
before SL on the same bar** (optimistic, no gaps). A trade that touched −3R before
reaching +5R is logged a win. So my `bracket AUC` numbers describe a *paper* win, not a
survivable one.

The fix is already available and unused:

```
nt_signal_sim.mae_4h_pct / 12h / 24h / 48h    (migration 170, fixed-horizon)
nt_signal_sim.mfe_4h_pct / 12h / 24h / 48h
```

### Tested — `worklog/kairos/reports/20261002_calibration_decomp.md` §1b

`label_survivable` was added to the instrument: a win counts only if TP was reached
**and** `mae_24h_pct` never breached the stop distance. Scored on identical rows
(6,279 with the MAE column):

| label | base rate | Brier | REL | RES | AUC |
|---|---:|---:|---:|---:|---:|
| BRACKET (`tp_hit`) | 0.2805 | 0.2698 | 0.0692 | 0.0016 | 0.4705 |
| SURVIVABLE (`tp_hit` AND `mae_24h < sl`) | **0.1186** | 0.2490 | **0.1443** | 0.0005 | **0.4746** |

**Two results, and they point different ways:**

1. **The ghost edge is real and large — as a level effect.** Of 1,761 bracket wins,
   **1,016 (57.7%) never survived** the stop distance. The survivable base rate is
   **0.1186**, not 0.2805. *Caveat:* the settlement label is a close-walk while the MAE
   column is intrabar highs/lows, so 57.7% is an **upper bound** — the true figure is
   between 0 and that, and pinning it needs an intrabar-consistent resolver. But the
   direction is not in doubt, and it is bigger than the attachment's qualitative claim.

2. **The ghost edge does NOT explain the discrimination failure.** AUC barely moves
   (0.4705 → 0.4746) while `REL` more than doubles. It is a **level** distortion, not a
   ranking one — precisely the `REL ≫ RES` pathology already measured. So the
   attachment's "false model confidence" is correct, but it is *not* the cause of the
   no-discrimination finding, and fixing it will not restore any.

**Net:** the attachment found a genuine defect in how the platform *labels* outcomes. It
is a base-rate and calibration problem, not a ranking problem. Worth fixing — the
survivable label is the honest one — but it does not change the conclusion that `p_win`
cannot rank, and it makes its overconfidence worse rather than better.

---

## 5. Where the attachment overstates

| Claim | Correction |
|---|---|
| "Promote reachability to live" | `dynamic_atr_mode` is **already live** (`feature_flags.yaml:410`); `heavy_sweep_atr_anchored_bricks_mode` is shadow with a **partial live hybrid** (`atr_anchored_evidence_mode: live` since 2026-09-25). Only `heavy_sweep_reachability_mode` is fully shadow. |
| "Use Kairos `E(C,a)` for entry gating" | The gate **does not exist**; `m_h` is measurement-only. And my `drift_*` analysis measures the same directional signal at **AUC 0.516** — weak. Gating on it before it clears a bar would be gating on noise. |
| "HMM entropy / shift velocity gating" | Neither exists. Presented as existing mechanisms; they are proposals. |
| "5 native mechanisms to calibrate without `P_win`" | **3 exist.** Of those, one is shadow and one (Masaniello) depends on `p_win` anyway. The honest count of `p_win`-free, live mechanisms is **1** (`V_i`, inside a path the sweep doesn't use). |

---

## 6. What is agreed, and the honest convergence

**Both analyses independently reach the same conclusion: stop calibrating on binary
`P_win`.** That agreement is now backed by measurement on this platform's own data, not
just by general practice.

They diverge on the replacement — and the divergence is complementary, not a conflict:

| | attachment | my measurements |
|---|---|---|
| question answered | **how to size** | **what probability to size on** |
| replacement | Masaniello / vol-targeting | calibrated reachability cell (~0.55 AUC) |
| gap | needs a trustworthy input | has no sizing vehicle |

**Synthesis — the actionable plan, in dependency order:**

1. **Close the MAE hole** (§4) — re-score against a survivable label. *Fixes an error in
   my own work; needs no new data.*
2. **Calibrate the cell probability** (isotonic, rolling OOS, frozen before evaluation) —
   the object the attachment assumes exists and mine says is only ~0.55.
3. **Feed it into Masaniello's `prob_factor`**, keeping `M_i`/`DD_i`/`V_i` intact — this
   is where the two analyses actually meet.
4. **Raise `min_prob` off 0.50** or make it probability-aware — the audit's E7 floor is
   inherited otherwise.
5. Only then consider promoting `reachability_aware_pick` to live.

**What neither analysis has:** enough history. 40 days (2026-08-23 → 2026-10-02,
verified) bounds steps 2–5. Step 1 can be done today.

---

## 7. Open items

1. **MAE-conditioned re-scoring** — the one immediate, no-new-data test. *Recommended next.*
2. **`min_prob = 0.50`** — inherited floor; needs a decision independent of Masaniello.
3. **Masaniello in the sweep path** — it is a portfolio-level path (`routers/sizing.py`);
   wiring it into the sweep is new integration, not a config flip. The attachment implies
   a flag change; it is not.
4. **`use_kelly_overlay`** — if the goal is to *deprecate* Kelly, this must be off, and
   `prob_factor` must stop reading `p_win`.
5. **Kairos `E(C,a)` gate** — build only after the directional signal clears a bar.
