# L1 Confident Calibration — Recommendations in Lieu of the Past Three Weeks

> **Date:** 2026-09-22 · **Type:** product/engineering recommendations (assessment only — no code)  
> **Layer:** L1 Product — *is `p_win` honest? does the book earn its risk?*  
> **Parent:** `worklog/20260922_QUALITY_MEASUREMENT_LAYERS_ASSESSMENT.md` (measurement model)  
> **Why now:** ~2026-09-01 → 2026-09-22 shipped a large stack of settlement, geometry, gate, and measurement fixes. Live calibration is **still** overconfident on several symbols. This worklog states what “confident calibration” must mean on the product, and what has to happen *after* those changes — not another re-litigation of them.

---

## 0. Definition of done — “confident calibration” (L1)

The product is **confidently calibrated** only when all four hold on the **qualified live book**:

| # | Claim | Evidence required |
|---|---|---|
| **C1 Honesty** | Stated `p_win` (or `p_win_bracket`) matches realized outcome frequency | Per-cell bias ≈ 0; reliability curve diagonal within CI |
| **C2 Discrimination** | Higher stated `p_win` actually means higher win rate | Brier skill > 0 vs base rate; not just a shrunken 0.5 |
| **C3 Stability** | Claims hold across time windows and side (buy/sell), not one lucky week | Rolling 7d/28d/episode windows agree within tolerance |
| **C4 Actionability** | When miscalibrated, the product *acts* (shrink / cut Kelly / refuse) without starving its own evidence | Enforcement + minimum-episode guards proven; no silent fallbacks |

Anything short of C1–C4 is **calibration theater** — a bias number that looks like progress while EV/Kelly still price the wrong event.

**Not in L1:** win% or expiry as product KPIs (those are L3/L2 diagnostics).  
**L1 surfaces only:** paper book PnL (aligned units) + calibration honesty + Brier/reliability.

---

## 1. What the past three weeks already changed (inventory — do not redo)

This is the “in lieu of” baseline. Each row is live or explicitly parked as of 2026-09-22.

| Window | Shipped / decided | Effect on calibration |
|---|---|---|
| 09-01 | Regime label stability scope → `regime_label_debounce_mode` (now **live** 09-22) | Fewer flip-flop labels → cleaner direction episodes |
| 09-04 | Kelly **direction-aware** sizing + signal-engine math triage (Masaniello, fractional Kelly, `kelly_edge`, `_mu`) | Sizing no longer direction-blind; EV inputs less systematically wrong |
| 09-04 | MIN_RR single source (`feature_flags.yaml` 1.5) | Light RR clause is inert; not a quality lever |
| 09-08 | Pricing/scoring root cause: **episode-counted** bias, MTM expiry PnL, deadline-aware selection | Bias no longer counts re-samples as independent trials |
| 09-09 | Calibration vs scoring bias reframe; `p_historical` / dual-correction overlap named | Conceptual split is documented; dual correction still needs one owner |
| 09-13 | Vol-tier reachability keying `(symbol, vol_tier, distance/ATR)` | Correct grain for honesty cells |
| 09-14 | Shadow-composition leak fixed (reachability/ATR/deadline) | Measurement windows no longer self-contaminated |
| 09-16 | Streamline batches A–D+F: settlement contract, raw-only Brier, proxy-ATR quarantine, `p_win_bracket` shadow, dual-report scorecard | Measurement honesty path largely built |
| 09-16 | Reachability bootstrap (`n_stored` rebuild) | Unblocked `p_win_bracket` scoring |
| 09-18 | Tiered ATR geometry scope | Wide-bracket problem named as **geometry**, not “weak direction” |
| 09-19 | Heavy geometry promote + `predicted_p_win_raw` on heavy path; intraday geometry_tuning (**live**) | Heavy rows enter calibration with raw; geometry can correct without waiting on bias |
| 09-20 | Qualified quality gates + macro hygiene; dynamic MIN_RR superseded | Gates composition is kelly/regime/p_win — not RR |
| 09-22 | `trend_probs_gate_mode` + `regime_label_debounce_mode` → **live** | Gated Brier beat raw 14/14; EV uses gated `p_regime` |
| 09-22 | Migration 159 side views (`v_signal_outcome_by_side`, `v_reachability_side_gap`) | Buy/sell geometry and outcomes are measurable |

**Still shadow / incomplete for L1:**

| Item | Mode today | Why L1 still cares |
|---|---|---|
| `p_win_bracket_mode` | **shadow** (`min_p_win_bracket: 0.45`) | Gates/EV still use **directional** `p_win` — C1 fails by construction |
| `markov_source_mode` | `trend_hmm_shadow` | Legacy unstable HMM still feeds `p_markov_hold_n` |
| Calibration dual-correction | Documented, not single-owned | Risk of over-shrink / evidence starvation |
| Paper vs equal-weight | Units incommensurate; symbol view broken | C4 book proof unusable |
| Reliability / Brier cells | Partial (raw capture yes; per-cell board thin) | C1–C3 cannot be signed off |

---

## 2. Why bias still looks worse after “all that work”

Honest reading of the 09-15 retrospective + 09-22 live board:

1. **The prediction target is still wrong.** Directional `p_win` is scored against bracket resolution. Geometry work (tiered ATR, reachability, geometry_tuning) improves *reach*, but until EV uses `p_win_bracket`, overconfidence on wide/low-touch brackets remains structural.
2. **Expiry is not “failure” and not “ignored.”** Deadline-aware + MTM made expiry *visible*. Bias that only counts `tp_hit`/`sl_hit` (survivorship) can look fine while the book never resolves — or look worse once resolution rate rises and losses enter the sample. That is not a regression; it is less cheating.
3. **One system-wide bias is the wrong resolution.** Per-symbol/side/regime cells move opposite directions (AAPL buy in `high_vol_strong_bull`; XAGUSD macro; gold pairs). A blended +0.27 → +0.34 can hide improving cells and failing cells at once.
4. **Enforcement is active while honesty is still thin.** `EOD_CALIBRATION_ENFORCE` shrinks p_win and cuts Kelly when |bias| > 0.10. That can *mask* model miscalibration in the live stream (enforced p_win looks better) while `predicted_p_win_raw` still disagrees with outcomes. L1 must measure **raw honesty**, not enforcement-adjusted comfort.
5. **Thin episode counts.** `calibration_bias_min_episodes: 2` is too low for confidence. Symbols with 1–5 episodes cannot support C1–C3.

---

## 3. Target model (what L1 should price)

```text
  raw sources (p_regime gated, p_markov, p_timesfm, …)
           │
           ▼
     p_dir  = P(direction correct)          ← rename of today's p_win
           │
           ×  L3 lookup
           │    p_resolve     = P(TP or SL by deadline | symbol, vol_tier, bucket, side)
           │    p_win|resolve = Beta-Binomial shrink of tp/(tp+sl) in cell
           ▼
  p_win_bracket = p_resolve × p_win|resolve
           │
           ├──► EV = p_win_bracket·profit − (1 − p_win_bracket − p_expire)·loss
           ├──► Gate: p_win_bracket ≥ min_p_win_bracket (shadow → live)
           └──► Kelly on p_win_bracket (not p_dir)
           │
           ▼
  Reliability board: bias / Brier / n_episodes per
  (symbol, side, vol_tier, bucket) on QUALIFIED book only
```

**Non-negotiable (from 09-16 rules, restated for L1):**  
Same event · one settlement · measurement first · fail closed on raw · shadow flips only on named replay.

---

## 4. Recommendations — what must still happen

Ordered so each step is independently shippable and improves trust even if the next waits.

### R0 — Freeze the L1 scoreboard (1–2 days, no model change)

**What:** One admin/board surface that is the *only* place “is calibration confident?” is answered.

| Column | Definition |
|---|---|
| `n_episodes` | Episode-clustered, not raw ticks |
| `p_win_raw` | Pre-enforcement |
| `p_win_enforced` | Post-enforcement (diagnostic only) |
| `win_rate_closed` | tp/(tp+sl) |
| `pct_expired` | Geometry diagnostic, not loss |
| `bias_raw` | `avg(p_win_raw) − win_rate_closed` on same population |
| `brier_raw` | On resolved; `error_source=raw_missing` never silent |
| `cell` | `(symbol, side, vol_tier, distance_atr_bucket)` |
| `status` | INSUFFICIENT / MISCALIBRATED / CALIBRATED by **tighter** bands |

**Accept:** Board reads live Supabase; gold/crypto/FX cells that are OVERCONFIDENT at 09-22 are visible as first-class failures. No blended-only view.

**Also fix the status band:** today +0.23–0.30 can still stamp CALIBRATED. For “confident,” require |bias| ≤ **0.05** with n_episodes ≥ **10**, or label **PROVISIONAL**.

---

### R1 — Split `p_dir` vs `p_win_bracket` in product language (same week)

**What:**  
- Telemetry/UI: `p_win` → `p_dir` (“direction confidence”) until bracket is live.  
- User copy never claims “probability this trade wins” from directional confidence.  
- Dual-write both on `nt_signal_sim` (already partially done via migration 154 / Batch C).

**Accept:** Grep/docs/UI show the two names; no marketing or Discord copy sells `p_dir` as win probability.

**Why first after R0:** Prevents the product from *asserting* calibration it has not earned while R2–R4 run.

---

### R2 — Close the event gap: promote `p_win_bracket` shadow → live (the core L1 fix)

**Depends on:** reachability `n_stored` healthy (bootstrap 09-16), geometry_tuning live (09-19), side cells (159).

| Step | Action | Gate |
|---|---|---|
| 2a | Run `scripts/replay_p_win_bracket_gate.py` on last 28–70 days | Table: would-reject rate, closed win%, Brier vs current gates |
| 2b | Shadow gate only: log `would_reject_bracket` | ≥2 weeks clean logs; no dead symbols from thin cells (fail-open documented) |
| 2c | EV uses `p_win_bracket` (`p_win_in_ev_mode`) | Replay expectancy improves or flat while |bias| falls |
| 2d | Live gate `min_p_win_bracket` (start 0.45) | Qualified book avg `p_win_bracket` ↑; |bias_raw| ↓ |
| 2e | Kelly on bracket probability | Paper risk-per-trade does not explode |

**Accept for “confident” claim:** On qualified book, **median cell |bias_raw| ≤ 0.05** and Brier skill > 0, with n_episodes ≥ 10 covering ≥70% of volume.

**Do not** flip live because the calendar hit Oct 5 — flip on replay evidence (09-16 rule).

---

### R3 — Make calibration cells the enforcement input (not one blended bias)

**What:**  
1. Keep episode counting (09-08).  
2. Compute bias **per (symbol, side, vol_tier, bucket)** with Beta prior shrink toward 0.5 (α=β=2 as in `p_win_bracket`).  
3. Enforcement (`bayesian_penalty`, Kelly half) reads the **cell** bias when n sufficient; else parent symbol; else system — explicit hierarchy, logged.  
4. **One owner** for correction: retire the overlapping soft `p_historical` vs hard `apply_calibration_adjustment` stack (09-09 reframe) to a single shrink path + documented residual.  
5. Underconfidence ≠ overconfidence: do **not** Kelly-cut both ways the same (09-16 4.6).

**Accept:** Replay `replay_calibration_gap.py` shows cell-level bias; no symbol is shrunk because a different symbol is overconfident; USDCHF-class “100% of sweeps fail p_win_ok” crush from 09 history does not reappear without a cell justification.

---

### R4 — Expire in the model, not only in the scorecard

**What:** Treat  
`P(win) = P(resolve) × P(win|resolve)`  
as the **only** calibrated object for EV/Kelly/gates.

- `pct_expired` / long-lag / `tp_atr` feed **geometry_risk** and `p_resolve` (L3), not a fake loss label.  
- Dual-report remains for transparency (`pnl_closed_only` vs `pnl_all_resolved`).  
- High-expiry cells (EURUSD/GBPUSD/AUDUSD class at 09-22: 92–100% expire) are **geometry defects** — they fail C1 even if closed-only bias looks fine.

**Accept:** No L1 “calibrated” badge on a cell with `pct_expired > 0.40` until `p_resolve` is in the published probability and geometry_tuning has run.

---

### R5 — Book proof the product can trust (paper vs baseline)

**What:** L1 confidence includes “does honest p_win produce honest PnL.”

1. Align units: same notional (e.g. $200 / 0.01 lot), same cohort (`qualified=true`), same expired treatment.  
2. Rewrite `v_paper_vs_optimized_symbol` as true per-symbol aggregate (kill per-signal LATERAL explode — 457 `n=1` rows at 09-22).  
3. Headline: **paper_cum**, **last-28d paper**, **paper − baseline**, **calibration board** — not research-stream `equal_wt_pnl` (−$412k artifact).  
4. Fail closed: missing `predicted_p_win_raw` on any book trade is an error, not a skip.

**Accept:** One chart: rolling 28d paper PnL next to median |bias_raw| — when bias shrinks, PnL quality should not be an accounting trick.

---

### R6 — Side honesty (buy vs sell) inside every L1 claim

**What:** Migration 159 already splits outcomes/reach. L1 must not publish a single symbol bias that mixes sides when the live book shows 09-22 asymmetry (qualified buy `win_closed` 30.7% vs sell 20.4%).

- Reliability and enforcement: **by side** when n ≥ 5 episodes.  
- Product co-movement stays in the **pairs matrix** (commentary symbols) — that is risk UX, not calibration.  
- Investigate mirror gaps (`tp_gap ≈ −sl_gap`) as **bracket orientation** before treating them as alpha.

**Accept:** Board and enforcement both have a `signal` dimension; no “AAPL is calibrated” row that averages buy losses with sell noise.

---

### R7 — Evidence quality floor (C3 stability)

| Rule | Value |
|---|---|
| Min episodes to claim CALIBRATED | **10** (not 2) |
| Min episodes to enforce shrink | 5 (else PROVISIONAL, fail-open size) |
| Windows | 7d + 28d + full post-fix epoch (≥ 09-16) |
| Quarantine | `atr_source=proxy_*`, shadow-leak windows, heavy rows missing raw |
| Trust flag | Any shadow metric feeding a decision must expose `data_window_flagged` (retro 09-15 scorecard item) |

**Accept:** “Confident calibration” badges are **absent** on thin cells by default — absence is honest.

---

### R8 — Model sources only after C1 (optional, late)

Do **not** chase `trend_hmm_live` / markov retirement / EV weight retunes as an L1 calibration fix. Those are **C2 discrimination** improvements after honesty is stable.

Order of model work *after* R2–R4 pass:

1. `markov_source_mode: trend_hmm_live` (if shadow Brier still wins)  
2. EV source weights (only on cells that already pass C1)  
3. Meta-coherence / fifth source (09-09 proposal) — research only until board is green  

---

## 5. Suggested sequence (product)

```text
Week 0     R0 scoreboard + status band          ── trust surface
           R1 p_dir rename / copy honesty       ── stop overclaiming

Week 1–2   R2a–2b bracket gate replay + shadow ── event gap
           R3 cell bias + single correction     ── right unit of truth
           R6 side dimension on board           ── buy/sell honesty

Week 2–3   R2c–2d EV/gate live on p_win_bracket
           R4 expire in model (p_resolve)
           R5 paper baseline rewrite

Week 3+    R7 evidence floor enforcement
           R8 model source work (only if board green)
```

Severity discipline (AGENTS.md): measurement honesty (R0/R1/R5 raw fail-closed) can ship as **critical/high** without waiting; live gate flips (R2d) are their own green-light batch.

---

## 6. Definition of “L1 confident calibration” for product go/no-go

**Green (can claim confident calibration on the book):**

- [ ] Board is per-cell, episode-based, raw-first (R0/R7)  
- [ ] Published probability is `p_win_bracket` in EV + gates (R2)  
- [ ] Median |bias_raw| ≤ 0.05 and Brier skill > 0 on qualified cells with n≥10 (R2/R7)  
- [ ] No CALIBRATED badge when `pct_expired > 0.40` without `p_resolve` in-model (R4)  
- [ ] Paper vs baseline is unit-aligned and cohort-aligned (R5)  
- [ ] Side-split enforcement and reporting (R6)  
- [ ] Single correction owner; no silent enforced-as-raw (R3, Batch B rules)

**Red (must not claim):**

- Blended system bias alone  
- Closed-only bias with unmanaged 90%+ expiry cells  
- Enforcement-adjusted p_win used as proof of honesty  
- Research-stream EOD win%/expiry as product quality  
- “Calibrated” on n_episodes < 10  

---

## 7. Live reference snapshot (2026-09-22) — the gap R0 must show

| Symbol | n_ep | bias | status (old) | Honest L1 call |
|---|---:|---:|---|---|
| XAUUSD | 3 | +0.46 | OVERCONFIDENT | FAIL C1 — gold geometry + overconfidence |
| EURUSD | 5 | +0.45 | OVERCONFIDENT | FAIL C1 + C4 (92–100% expire cells) |
| XAGUSD | 2 | +0.38 | OVERCONFIDENT | FAIL C1 / C7 thin n |
| USDJPY | 2 | +0.39 | OVERCONFIDENT | FAIL C1 / C7 |
| BTCUSD | 2 | +0.36 | OVERCONFIDENT | FAIL C1 / C7 |
| US30USD | 4 | +0.23 | CALIBRATED* | PROVISIONAL — band too loose |
| AAPL | 5 | −0.02 | CALIBRATED | Best cell — still needs side/regime split |
| US500 | 3 | +0.05 | CALIBRATED | Borderline honest; n thin |

\*Tightened band would not call +0.23 “calibrated.”

---

## 8. Explicit non-goals (in lieu of past three weeks)

| Non-goal | Why |
|---|---|
| Re-open MIN_RR as quality gate | Settled 09-20: inert on light path |
| Raise light min_rr to 2.0 | Geometry choke, not honesty |
| Macro regime as calibration lever | 09-20: shadow labels, little live impact |
| Another system-wide bias target (e.g. “get to 0.0”) | Wrong resolution; enables shrink abuse |
| Rebuild settlement/deadline/episode work | Shipped; use it |
| Parked ATR-dynamic/conviction thesis | Still waits on reachability trust — not L1 honesty |

---

## 9. Acceptance tests (when Build Code is authorized)

| ID | Test |
|---|---|
| T1 | Fixture cells: bias_raw ≈ 0 when p_win_raw matches win_rate_closed |
| T2 | Missing raw → `error_source=raw_missing`, never falls back to enforced for Brier/bias |
| T3 | Episode clustering: 20 re-samples of one move = 1 episode |
| T4 | Side split: buy and sell biases independent under enforcement |
| T5 | Thin n: status PROVISIONAL / INSUFFICIENT, no shrink, fail-open size |
| T6 | `replay_p_win_bracket_gate.py` before/after table checked into worklog |
| T7 | Paper vs baseline same units same cohort (regression on view SQL) |
| T8 | Expire-heavy cell cannot badge CALIBRATED without p_resolve |

---

## 10. Bottom line

The past three weeks made the **measurement and selection substrate** trustworthy (episodes, deadline, vol-tier, raw capture, geometry hooks, live gated p_regime). They did **not** yet make the **product probability** honest — EV/gates still price direction as bracket win.

**Confident calibration (L1) = honest `p_win_bracket` on the qualified book, proven per cell (with side and geometry), enforced from those cells, and mirrored in unit-aligned paper PnL.**  
Everything else in this worklog is sequencing to get there without lying with a single bias number.

---

## References

- `worklog/20260922_QUALITY_MEASUREMENT_LAYERS_ASSESSMENT.md` — L1/L2/L3 measurement model + live board  
- `worklog/20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md` — batches A–F, target diagram  
- `worklog/20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md` — bias vs scoring bias; independence  
- `worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md` — episode counting, deadline-aware, MTM  
- `docs/20260915_TWO_WEEK_RETROSPECTIVE_SIGNAL_QUALITY_REVIEW.md` — why blended bias got worse  
- `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md` — category error in p_win  
- `docs/ACTIVE_MODES.md` + `config/feature_flags.yaml` — current modes (`p_win_bracket_mode: shadow`)  
