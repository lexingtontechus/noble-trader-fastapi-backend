# RM-1 evaluation — `markov_source_mode` (Change B shadow)

**Date:** 2026-10-01 · **Roadmap item:** RM-1 (`markov_source_mode`)
**Review due:** 2026-10-05 · **Review condition:** `Tier1 hit-rate vs legacy HMM comparison (nt_hmm_shadow_accuracy)`
**Live config:** `config/feature_flags.yaml:190` → `markov_source_mode: trend_hmm_shadow`
**Origin:** `worklog/20260825_CHANGE_B_MARKOV_SHADOW_MODE.md` (build), `20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md` (Tier 1/2 fixes)

---

## 0. What RM-1 is

`p_markov_hold_n` is one of four EV Engine v5 sources (weight 0.20). It reads a
3×3 `{UP,DOWN,FLAT}` transition matrix. Two candidates can supply it:

| Mode | Source | Numerical state |
|---|---|---|
| `legacy_hmm` | standalone 5-state `HmmModel` (`services/hmm/model.py`), 9 **unscaled** features | known unstable — `startprob_ must sum to 1 (got nan)` |
| `trend_hmm_live` | `RegimeHMM.trend_transition_matrix_3state()` — already fitted, sanitized, feature-standardized | stable |

`trend_hmm_shadow` computes both, logs the comparison, and **still feeds legacy**
to the live blend. RM-1 is the decision on whether to flip to `trend_hmm_live`
(and then B5: delete the legacy model).

---

## 1. Evidence — accuracy comparison (the review condition)

`nt_hmm_shadow_accuracy`, single snapshot **2026-10-01 01:19:21 UTC**, 14 symbols.

| symbol | legacy acc | legacy n | legacy cov | trend acc | trend n | trend cov | winner |
|---|---:|---:|---:|---:|---:|---:|---|
| AAPL | 0.4903 | 1087 | 0.186 | 0.2140 | 916 | 0.157 | legacy |
| AUDUSD | 0.6279 | 301 | 0.371 | 0.5136 | 479 | 0.591 | legacy |
| BTCUSD | 0.3503 | 571 | 0.123 | 0.2858 | 1319 | 0.284 | legacy |
| EURUSD | 0.5424 | 271 | 0.345 | 0.4861 | 576 | 0.734 | legacy |
| GBPUSD | 0.5042 | 355 | 0.334 | 0.3575 | 702 | 0.660 | legacy |
| US30USD | 0.5553 | 805 | 0.338 | 0.3311 | 1326 | 0.557 | legacy |
| US500 | 0.5289 | 675 | 0.328 | 0.2850 | 1505 | 0.731 | legacy |
| USDCAD | **0.7443** | 481 | 0.362 | 0.4034 | 833 | 0.627 | legacy |
| USDCHF | 0.5156 | 450 | 0.373 | 0.3752 | 829 | 0.687 | legacy |
| USDJPY | 0.4039 | 1072 | 0.434 | 0.4046 | 1525 | 0.618 | **trend_hmm** |
| XAGUSD | 0.5777 | 1667 | 0.319 | 0.2492 | 1625 | 0.311 | legacy |
| XAUAUD | 0.5690 | 1102 | 0.217 | 0.3588 | 1965 | 0.388 | legacy |
| XAUEUR | 0.4965 | 1007 | 0.238 | 0.2781 | 2237 | 0.529 | legacy |
| XAUUSD | 0.5582 | 867 | 0.211 | 0.2429 | 2104 | 0.513 | legacy |

**Weighted:** legacy `acc 0.5274` (n=10,711) vs trend_hmm `acc 0.3191` (n=17,941).
**legacy wins 13/14 symbols.** trend_hmm edges USDJPY by 0.0007 — a tie, not a win.

This is unchanged from the 2026-09-22 read (legacy won 13/14 then too, USDJPY the
only trend edge). **No convergence in 9 days.**

## 2. Evidence — state disagreement

`nt_markov_source_shadow` (running aggregate, updated every light tick):

| symbol | n_compared | n_disagree | disagree rate | last legacy | last trend |
|---|---:|---:|---:|---|---|
| AAPL | 4473 | 1896 | 0.424 | DOWN | FLAT |
| AUDUSD | 4565 | 3011 | 0.660 | FLAT | DOWN |
| BTCUSD | 4562 | 2442 | 0.535 | FLAT | FLAT |
| EURUSD | 4562 | 2976 | 0.652 | DOWN | DOWN |
| GBPUSD | 4564 | 3440 | 0.754 | FLAT | DOWN |
| US30USD | 4580 | 2828 | 0.618 | FLAT | DOWN |
| US500 | 4581 | 3517 | 0.768 | FLAT | DOWN |
| USDCAD | 4569 | 3266 | 0.715 | UP | FLAT |
| USDCHF | 4595 | 3098 | 0.674 | FLAT | UP |
| USDJPY | 4565 | 2759 | 0.604 | FLAT | DOWN |
| XAGUSD | 4587 | 3274 | 0.714 | DOWN | FLAT |
| XAUAUD | 4566 | 3128 | 0.685 | FLAT | FLAT |
| XAUEUR | 4579 | 3342 | 0.730 | FLAT | DOWN |
| XAUUSD | 4571 | 3114 | 0.681 | FLAT | FLAT |
| **TOTAL** | **63,919** | **42,091** | **0.6585** | | |

**65.9% disagreement** against a 3-class random baseline of 66.7%. The two
classifiers are **statistically indistinguishable from independent random
agreement**. This is *worse* than the 67.9% reported on 2026-08-27 — no
improvement over 5 weeks of accumulation.

## 3. Evidence — Tier 1/2 gating worked (Brier)

`raw` vs `gated` Brier on the same n per symbol (accuracy is NULL on these rows
by design — the gate is a probability calibration, not a hit-rate):

| symbol | n | raw Brier | gated Brier | Δ |
|---|---:|---:|---:|---:|
| AAPL | 2972 | 0.7064 | 0.3420 | **−0.364** |
| AUDUSD | 604 | 0.4946 | 0.3614 | −0.133 |
| BTCUSD | 2265 | 0.4720 | 0.4208 | −0.051 |
| EURUSD | 575 | 0.5306 | 0.4340 | −0.097 |
| GBPUSD | 581 | 0.5682 | 0.4414 | −0.127 |
| US30USD | 1634 | 0.5571 | 0.4668 | −0.090 |
| US500 | 1380 | 0.5520 | 0.4904 | −0.062 |
| USDCAD | 1044 | 0.6341 | 0.4571 | −0.177 |
| USDCHF | 852 | 0.5787 | 0.4985 | −0.080 |
| USDJPY | 2109 | 0.5018 | 0.4079 | −0.094 |
| XAGUSD | 2473 | 0.5565 | 0.4288 | −0.128 |
| XAUAUD | 2733 | 0.5344 | 0.4195 | −0.115 |
| XAUEUR | 2622 | 0.6191 | 0.5387 | −0.080 |
| XAUUSD | 2392 | 0.5948 | 0.5312 | −0.064 |

The magnitude/sign gate (Tier 1/2, 2026-08-26/27) improves Brier on **14/14
symbols**. That part of the work is validated.

## 4. Reading

1. **The shadow comparison has answered its question — negatively.** legacy
   beats trend_hmm on accuracy 13/14, and the two classifiers agree at
   random-chance rate. There is no evidence that flipping to `trend_hmm_live`
   would improve `p_markov_hold_n`.

2. **The 2026-09-20 user policy already covers this.** The stated position is
   that trend_hmm is *not required to beat* legacy — the context stack
   (15-min cadence, macro regime, debounce) is the intended hedge against
   markov instability, not a horse race. Under that policy, the accuracy result
   is not a blocker; it's simply not a reason to flip either.

3. **The real problem is that neither source is informative.** legacy accuracy
   0.527 weighted — barely above a 3-class coin flip. trend_hmm 0.319 — well
   below. Both are weak; the gate only rescues *calibration*, not *signal*.

4. **The shadow comparison is not free.** It computes both models every tick and
   writes to `nt_markov_source_shadow`. Two `record_signal_stream_meta_shadow`
   ReadTimeouts appeared in today's log — consistent with insert-load, which the
   2026-09-22 note already flagged as a candidate for retirement.

## 5. Where we are

| Aspect | State |
|---|---|
| Build (Change B, shadow) | ✅ complete, live since 2026-08-25 |
| Tier 1 magnitude gate (markov path) | ✅ built + validated (Brier Δ on 14/14) |
| Tier 2 gate (`trend_probs`/`p_regime`) | ✅ built 2026-08-27 |
| B5 legacy deletion | ❌ not started (gated on a live flip) |
| **Decision: flip to `trend_hmm_live`?** | ❌ **not justified** — legacy wins 13/14, 65.9% disagreement |
| Diagnostic value of shadow compare | ⚠️ exhausted — same answer for 9 days / 5 weeks |
| Review date | 2026-10-05 (Stage 1 cluster) |

**Verdict: RM-1 is *built and answered*, not *pending*.** The mechanism works,
the evidence is in, and the answer is "keep shadow, do not flip." What remains is
a *disposition* decision on the shadow comparison itself — keep paying for it, or
retire it and leave `p_markov_hold_n` on legacy indefinitely.

## 6. Options for the 2026-10-05 review

| Option | What | Consequence |
|---|---|---|
| **A — Retire shadow → `legacy_hmm`** | Stop computing/logging the comparison; leave legacy feeding the blend | Cuts per-tick work + insert load; accepts the known-unstable legacy model as permanent; `p_markov_hold_n` stays weak |
| **B — Keep shadow, extend** | Leave as-is, re-review later | No decision; keeps paying for a comparison that has been stable for 5 weeks |
| **C — Flip live anyway** | `trend_hmm_live` on the stability argument, accepting lower accuracy | Contradicts the measured accuracy; only defensible under the 09-20 policy that accuracy is not the bar |
| **D — Retire both → drop `p_markov_hold_n`** | Remove the source from the EV blend, renormalize the 3 remaining | The honest option if neither source is informative (both ≈ 3-class coin flip); larger EV change, needs its own scope |

**Recommendation: A or D.** A is the low-risk cleanup; D is the real answer if
`p_markov_hold_n` is not earning its 0.20 weight. Both are the user's call —
this report does not flip anything.

---

### Reproduce

```bash
# accuracy (review condition)
curl "$SUPABASE_URL/rest/v1/nt_hmm_shadow_accuracy?select=symbol,metric,n,coverage,accuracy,brier_score,computed_at" \
  -H "apikey: $SUPABASE_KEY" -H "Authorization: Bearer $SUPABASE_KEY"

# disagreement (running aggregate)
curl "$SUPABASE_URL/rest/v1/nt_markov_source_shadow?select=*&order=symbol.asc" \
  -H "apikey: $SUPABASE_KEY" -H "Authorization: Bearer $SUPABASE_KEY"

# live mode
grep -n 'markov_source_mode' config/feature_flags.yaml
grep -c 'markov shadow' logs/orchestrator.log
```
