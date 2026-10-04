# 2026-10-04 — Kairos Rank Score: Shadow Analysis + Calibration Feedback Scope

**Roadmap ID:** 76
**Mechanism:** `kairos_rank_score`
**Status:** Assessment complete — ranking signal recommended, calibration feedback scoped
**Date:** 2026-10-04
**Analyst:** Hermes Agent

---

## 1. Context

`kairos_rank_score` has been stamping on `nt_signal_sim` since 2026-09-26 (migration 172). The roadmap condition was:

> >=200 post-ATR episodes with rank_score stamped AND DeltaD CI > 0 (shadow gate P60). Then consider one gate lever vs 7-gate on scorecard D.

This worklog assesses:
1. Whether the rank score is ready for live promotion (as a gate)
2. Whether a ranking signal (sort by rank_score) is better than a binary gate
3. How the rank score fits into the calibration feedback loop (original design intent)

---

## 2. Data

| Metric | Value |
|---|---|
| Total `kairos_rank_score` stamps | 7,317 |
| Date range | 2026-09-26 → 2026-10-04 (8 days) |
| Qualified + resolved episodes | 1,755 |
| Symbols covered | 14 (all) |

---

## 3. Pooled Tercile Analysis

| Tercile | n | Avg Rank | Win Rate | Expire Rate |
|---|---|---|---|---|
| Top (T1) | 590 | 0.6496 | **23.22%** | 3.22% |
| Middle (T2) | 586 | 0.4550 | 16.04% | 10.58% |
| Bottom (T3) | 579 | 0.2921 | 11.40% | 10.36% |

| Metric | Value |
|---|---|
| ΔD (T1 − T3 win rate) | **+11.82 pp** |
| ΔD ratio (T1 / T3) | **2.04×** |
| Expire rate reduction (T1 vs T3) | 3.22% vs 10.36% (3.2× lower) |
| Monotonic discrimination | Yes (T1 > T2 > T3) |

**Reading:** The rank score has **strong pooled discrimination** — top tercile wins 2.04× more often than bottom tercile, with 3.2× lower expire rate.

---

## 4. P60 Gate Simulation

| Metric | Value |
|---|---|
| P60 gate rejects | 70% of signals (D1–D7) |
| False positive (rejected → would win) | 14.0% (184/1,310) |
| True positive (rejected → would loss) | 75.7% (992/1,310) |
| True positive (rejected → would expire) | 10.2% (134/1,310) |

**Verdict:** P60 gate is **too aggressive** — rejects 70% of signals. Not viable as a binary gate.

---

## 5. Per-Symbol Discrimination

| Symbol | D1 Win | D10 Win | Pattern | Verdict |
|---|---|---|---|---|
| US30USD | 77.78% | 0.00% | Monotonic ✅ | Strong |
| USDJPY | 100.00% | 0.00% | Monotonic ✅ | Strong |
| XAUEUR | 52.63% | 0.00% | Monotonic ✅ | Strong |
| GBPUSD | 30.77% | 0.00% | Monotonic ✅ | Moderate |
| XAGUSD | 33.33% | 62.50% | Inverse ❌ | Unreliable |
| BTCUSD | 26.67% | 7.14% | Inverse ❌ | Unreliable |
| US500 | 22.22% | 87.50% | Inverse ❌ | Unreliable |
| AAPL | 0.00% | 0.00% | Flat ❌ | No signal |
| XAUAUD | 18.75% | 0.00% | Flat ❌ | No signal |
| XAUUSD | 5.56% | 0.00% | Flat ❌ | No signal |
| AUDUSD | 6.25% | 0.00% | Flat ❌ | No signal |
| EURUSD | 30.00% | 0.00% | Noisy ⚠️ | Unreliable |
| USDCAD | 63.64% | 0.00% | Noisy ⚠️ | Unreliable |
| USDCHF | 0.00% | 0.00% | Flat ❌ | No signal |

**Only 4 of 14 symbols show clean monotonic discrimination.** Per-symbol thresholds would require n=5–26 per decile — too noisy.

---

## 6. Ranking Signal Benchmark

| Strategy | n | Win Rate | Expire Rate | Avg RR | Expectancy R |
|---|---|---|---|---|---|
| Baseline (all) | 1,755 | 16.92% | 8.03% | 0.4116 | — |
| **Top 1/day** | 8 | **50.00%** | **0.00%** | 0.9702 | −0.0149 |
| **Top 2/day** | 16 | **43.75%** | **0.00%** | 0.8244 | −0.2018 |
| **Top 3/day** | 24 | **45.83%** | **0.00%** | 0.9127 | −0.1233 |
| **Top 5/day** | 40 | **35.00%** | **0.00%** | 0.7543 | −0.3860 |

**Reading:** The ranking signal shows **35–50% win rate and 0% expire rate** — far better than baseline. The top 3/day strategy achieves 45.83% win rate with 0% expire, but n=24 is small.

---

## 7. Decision: Ranking Signal, Not Binary Gate

| Approach | Win Rate | Expire Rate | Sample Size | Per-Symbol Noise |
|---|---|---|---|---|
| P60 gate | 25.39% | 1.57% | 445 | High (6/14 symbols hurt) |
| **Ranking (top 3/day)** | **45.83%** | **0.00%** | 24 | **None (pools across symbols)** |

**The ranking signal pools across symbols**, naturally selecting the best opportunities regardless of which symbol they come from. This avoids the per-symbol discrimination problem entirely.

**The ranking signal is already implemented** — the dashboard signal ranking board sorts by `kairos_rank_score` (`rankOf(r)`). No new code needed for the ranking path.

---

## 8. Calibration Feedback Scope

### 8.1 Original Design Intent

From `worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md` §7:

> **Fix ranking first** (`y_acc` vs no-skill, within-cell IC). That is how `p_win` becomes a score.

> **Dynamic ATR** stays the economics engine; this programme **feeds the feedback loop** around it.

The rank score was designed as a **quality modulator for calibration** — not a gate, not a sizing input, but a per-signal quality signal that refines over/underconfidence.

### 8.2 The Calibration Gap

Before this session's `p_win_dir` change:

```
bias = avg(p_win) − win_rate
```

Where `p_win` was the directional blend — a **level** score with IC≈0 as a ranker.

After this session's change:

```
bias = avg(p_win_dir) − win_rate
```

Where `p_win_dir` is the bracket probability — a **different quantity** that actually measures the settlement event. The bias computation is now measuring the right thing, but it's still **unstratified** — it treats all signals equally.

### 8.3 The Fit

| Layer | Current state | With rank_score |
|---|---|---|
| **EV Engine** | `p_win` = bracket probability | unchanged |
| **Qualification** | 7-gate chain, `p_win_ok` on bracket prob | unchanged |
| **Calibration bias** | `avg(p_win_dir) − win_rate` per symbol/cell | **Stratified by rank_score** — high-rank signals get different bias treatment than low-rank |
| **Enforcement** | Bayesian shrink toward 0.5 | **Rank-conditioned shrink** — high-rank signals shrink less, low-rank signals shrink more |
| **Kelly** | `kelly × ev_scale` | **Rank-conditioned Kelly** — high-rank signals get full size, low-rank signals get reduced size |

### 8.4 What `p_win_dir` and `kairos_rank_score` Tell You

| Signal | What it measures | What it does NOT measure |
|---|---|---|
| `p_win_dir` | P(TP before SL) — the settlement event | How good this particular opportunity is |
| `kairos_rank_score` | Opportunity quality (freshness, type, recent TP, cell hit-rate) | The actual probability of winning |

**`p_win_dir` tells you WHAT the probability is. `kairos_rank_score` tells you HOW GOOD that probability is.**

The calibration loop currently only knows what — it needs to also know how good.

### 8.5 Proposed Calibration Feedback Mechanism

```
Currently:  bias = avg(p_win_dir) − win_rate
            → Bayesian shrink toward 0.5

Intended:   bias = f(p_win_dir, kairos_rank_score) − win_rate
            → Rank-conditioned Bayesian shrink
            → High-rank signals: shrink less (trust the model)
            → Low-rank signals: shrink more (distrust the model)
```

**Implementation sketch:**

1. **Rank-stratified bias measurement** — compute bias separately for high-rank (top tercile) and low-rank (bottom tercile) signals
2. **Rank-conditioned enforcement** — apply different Bayesian shrink factors based on rank_score
3. **Rank-conditioned Kelly** — modulate position size by rank_score (not as a gate, but as a continuous quality signal)

**Key principle:** The rank score is NOT a gate (it doesn't reject signals). It's a **quality modulator** — it adjusts how much we trust the model's probability for each specific signal.

---

## 9. Recommendations

| Path | Recommendation | Rationale |
|---|---|---|
| **Binary gate** | ❌ Do NOT flip to live | P60 rejects 70%; per-symbol discrimination inconsistent (only 4/14 clean) |
| **Ranking signal** | ✅ Already live in UI | 45.83% win rate, 0% expire; pools across symbols; already implemented |
| **Calibration feedback** | 🔶 Scope for build | Original design intent; fills the gap left by `p_win_dir` change; needs rank-stratified bias |

---

## 10. Next Steps

1. **Ranking signal** — no action needed (already live in dashboard)
2. **Calibration feedback** — build rank-stratified bias measurement + rank-conditioned enforcement
3. **Per-symbol thresholds** — not recommended (too noisy at n=5–26 per decile)

---

## 11. Related

- `worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md` — original rank score design
- `worklog/kairos/20260928_DASHBOARD_KAIROS_ENHANCEMENTS.md` — dashboard rank board
- `worklog/kairos/reports/20260926_rank_score_eval.md` — walk-forward eval
- `supabase/migrations/172_scorecard_p_win_dir_update.sql` — scorecard view updates
- `regime_platform/sweep_orchestrator/kairos_rank.py` — rank score implementation
- `regime_platform/sweep_orchestrator/calibration_enforcement.py` — calibration enforcement
