# Tier 1 context-alignment gate — INVERTED on gold buys (ID45 investigation)

**Date:** 2026-10-10
**Scope:** `tier1_context_alignment_gate` (roadmap #45) was flagged for a live-flip review. A 14-day outcome check found the gate's reject-cohort (`context_aligned_ok=false`) has a **higher** win rate than the pass-cohort — the opposite of the gate's premise. This documents why, and what to do.
**Status:** Investigated. **Do not ship as a reject-gate.** Recommend retire-or-repurpose.

---

## 1. The headline (inverted)

Episode-deduped, last 14 days, closed (`tp_hit`/`sl_hit`) only:

| context_aligned_ok | n | tp | sl | WR_closed |
|---|---|---|---|---|
| **false** (gate would reject) | 540 | 146 | 394 | **27.0%** |
| **true** (gate would pass) | 12,746 | 2,592 | 10,154 | **20.3%** |

The cohort the gate is designed to *suppress* wins **6.7pp more often** than the cohort it passes. If shipped as designed (reject `false`), it would cut the better-performing book.

## 2. Where the inversion lives — it's not uniform

Splitting by symbol (only symbols with ≥20 false-cohort rows):

| symbol | n_false | WR_false | n_true | WR_true | sign |
|---|---|---|---|---|---|
| **XAUUSD** | 69 | **50.7%** | 426 | 18.3% | **INVERTED** |
| **XAUAUD** | 171 | **44.4%** | 353 | 17.8% | **INVERTED** |
| XAGUSD | 265 | 12.1% | 818 | 35.5% | expected (false worse) |
| BTCUSD | 35 | 8.6% | 1194 | 30.5% | expected (false worse) |

The inversion is **entirely gold** (XAUUSD, XAUAUD). Silver and BTC show the sign the gate was built for. So this is not "the gate is broken everywhere" — it's a specific, exploitable gold-buy pattern the gate misclassifies.

## 3. The mechanism (why gold buys invert)

`compute_context_alignment` (context_alignment.py) returns `false` only when **both** longitudinal reads confidently oppose the signal: `macro_label` ≠ implied AND `trend_label` points the other way. Checking what the false gold-buy rows actually are:

| macro_label | trend_label | signal | closed | tp | WR |
|---|---|---|---|---|---|
| bear | reversal_down | buy | 206 | 98 | **47.6%** |
| bear | trending_down | buy | 34 | 13 | **38.2%** |

Every inverted row is a **gold BUY taken while macro=bear and trend=down.** The 5-minute EV blend fired a buy, both slow filters said "downtrend," and the buy still won ~46% of the time. That is a **mean-reversion / trend-exhaustion** signal: when gold is oversold into a downtrend and the fast blend flips bullish, the counter-trend buy has real edge.

The reverse side confirms the asymmetry: **gold sells are always `context_aligned_ok=true`** (macro/trend never oppose a sell in a gold downtrend), so the gate *never fires on gold sells*. Its only gold action is to reject gold buys into a downtrend — precisely the cohort that wins.

## 4. Why it's still wrong to call the gate universally bad

XAGUSD and BTCUSD behave exactly as designed (misaligned signals lose). The gate's *logic* — "if both slow filters confidently oppose, don't qualify on the fast blend alone" — is sound for those. The failure is that it applies one rule across symbols whose market microstructure differ: gold mean-reverts off downtrend exhaustion; silver/crypto trend-continue. A direction+symbol-agnostic reject-gate cannot serve both.

## 5. Recommendations

1. **Do not ship `tier1_context_alignment_gate` as a reject-gate.** On the current evidence it cuts winners on gold (the largest single inversion) while helping silver/crypto. Net effect on a blended book is ambiguous-to-negative, and the gold harm is concentrated and large (50.7% vs 18.3%).
2. **Retire the blanket gate.** Keep `compute_context_alignment` as a *stamp* (it's already persisted on `nt_signal_sim`), not a veto.
3. **Repurpose candidate — gold-buy PROMOTE signal.** The false-cohort on XAUUSD/XAUAUD buy (macro=bear + trend=down + fast blend buys) is a high-WR mean-reversion setup (~46–50%, n≈240 combined). If promoted deliberately — not as a suppressed-then-rescued signal but as a first-class edge — it could *raise* gold-buy qualification instead of lowering it. Needs its own walk-forward + era-split validation before any live use (this is a 14-day window; mean-reversion edges are regime-fragile).
4. **Symbol-aware gate (alternative).** If a gate is wanted, make the reject direction conditional on symbol/asset-class: reject-false for silver/crypto, ignore-or-promote for gold. But per the platform's one-lever-at-a-time discipline, option 3 (study the gold-buy edge) is the higher-value next step.

## 6. Data lineage

- Population: `nt_signal_sim`, `source='sweep-light'`, `resolved_at > now() - 14d`, `outcome in ('tp_hit','sl_hit')`, `context_aligned_ok is not null`.
- Episode-dedup where used: `distinct on (symbol, signal, date_trunc('hour', ts))`.
- Roadmap: `tier1_context_alignment_gate_scope` updated to `pending` with this finding, review 2026-10-17. (There is no bare `_gate` row; the `_scope` row is the active one.)
