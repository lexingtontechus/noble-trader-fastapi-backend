# Envelope reconstruction — finding overturned; the real issue is sample thinness

**Date:** 2026-10-09
**Status:** **DIAGNOSIS OVERTURNED — do not build the reconstruction.** The envelope walk was proven faithful to the live resolver (identical outcomes + pnl across all symbols on an extended tp grid). The monotonic-in-tp signature is **real data**, not a defect. See §0.

---

## 0. OVERTURN — the envelope is correct; the reconstruction would fix nothing

I specced an envelope reconstruction below on the hypothesis that `build_envelopes`' independent `fav`/`adv` manufactured a phantom wide-TP edge. **I tested that hypothesis directly and it was wrong.**

**The test:** for each symbol, score the same era-clean signals two ways across an extended tp grid (sl=1.0) —
- `walk_envelopes` (independent extremes, the picker), and
- `resolve_bracket_path` (the live resolver's path walk, bar-by-bar OHLC first-passage).

**The result: identical to the last decimal, every symbol, every tp.** Example (XAGUSD, n=68):

| tp | env e | path e | P(tp) | P(sl) |
|---|---|---|---|---|
| 1.5 | +0.862 | +0.862 | 0.76 | 0.24 |
| 3.0 | +1.891 | +1.891 | 0.74 | 0.26 |
| 6.0 | +3.685 | +3.685 | 0.68 | 0.32 |

**Why they agree:** `resolve_bracket_path` itself uses the same `hi>=tp → exit at tp` / `lo<=sl → exit at sl` first-passage rule (settlement_contract.py:478). It is *also* discrete pnl. Both exit at the bracket level, never an intermediate price. So there is no "path-consistent intermediate exit" to recover — the reconstruction in §3 would reproduce the exact same numbers. It is busywork.

**What the monotonic-in-tp actually is:** a true property of the expectancy surface. For XAGUSD, P(tp) barely falls (0.76→0.68) as tp goes 1.5→6.0, while the payoff scales 4×. On a 48-bar intrabar window for a trending metal, price reaches +6 ATR nearly as often as +1.5 ATR, and a 1-ATR stop rarely triggers first. The pick at tp=6/sl=1 is **genuinely optimal on this sample** — not a grid-edge artifact.

**The real risk, restated:** the false-signal trap here is **not** a broken walk. It is **thin-sample / single-regime expectancy**: n≈68 per symbol, all from one trending week (post-2026-10-02), with the only robustness check being a ~34/34 time split that itself passes (e1,e2 both climb with tp). A tp=6/sl=1 structure (RR=6) that "wins" on 68 signals from one regime is the classic setup for a pick that collapses out-of-sample. **The fix is walk-forward validation and a min-sample/regime-diversity bar, not an envelope rebuild.**

---

<details><summary>Original (overturned) analysis — kept for the reasoning trail</summary>

**Date:** 2026-10-09
**Scope:** `tiered_atr.build_envelopes` / `walk_envelopes` (the dynamic-ATR geometry chooser) scores brackets against an envelope that is **not path-consistent**, so a single bar can simultaneously satisfy a wide TP and a deep SL. This manufactures an expectancy that is monotonic in TP width — the exact pattern that previously led the platform down the dynamic-optimization path on a false signal.
**Status:** Diagnosis complete; **not built.** Spec only. No backtest run (per instruction) — a replay on the current envelope would launder the defect into a "validated" result.

---

## 1. The finding (from the picker diagnostic, era-clean data)

Running `dynamic_atr_pick`'s own `build_envelopes` + `walk_envelopes` on post-cutover signals surfaced a signature that is not a data artifact:

- **Expectancy rises monotonically with TP width.** AUDUSD: e climbs 0.87 → 2.53 as tp goes 1.5 → 4.0; every passing symbol picks either `tp=4.0` (grid edge) or `tp=1.5/sl=1.0`. Passing picks are pinned at the grid boundary — a chooser that always wants the widest allowed target is telling on itself.
- **PnL is discrete** — always exactly `+tp`, `−sl`, or `0.0` — because `walk_envelopes` line 238 is `np.where(tp_first, tp_atr, np.where(sl_first, -sl_atr, 0.0))`.
- Four symbols (XAUUSD/XAUAUD/USDJPY/XAUEUR) return `None` with `e1 == e2 == e` identical — every signal resolved the same way in both halves.

### The mechanism (the defect)

`build_envelopes` (tiered_atr.py:196-199) builds the two excursions **independently**:

```python
up = (hi - entry) * d / atr      # from the bar HIGH
dn = (lo - entry) * d / atr      # from the bar LOW
fav = np.maximum(up, dn)         # more favourable of the two
adv = np.minimum(up, dn)         # more adverse of the two
```

`fav` and `adv` are two independent point extremes. **Nothing forces them to come from a coherent path**, so one bar can carry both a large favourable *and* a large adverse excursion. In a grid search that bar is reused across every (tp, sl) candidate: it satisfies `fav >= tp` **and** `adv <= -sl` at once, `walk_envelopes` applies `sl_first`, and the outcome collapses to discrete `-sl`. The pnl never reflects *where price actually was* — only which of two independent extremes crossed a threshold first. Widen TP and more bars flip from "SL" to "TP-first" without any change in the underlying path, so expectancy climbs with tp. That is the monotonic signature.

### Why this is a chooser defect, not a resolver defect

`settlement_contract._excursions` (settlement_contract.py:555-558) builds `fav`/`adv` the **same independent way** — and there it is *correct*, because `resolve_bracket_from_envelope` (line 586-589) consumes it **once per signal** with the actual tp/sl, resolves first passage, and resolves same-bar ambiguity by `same_bar_policy` while stamping `ambiguous`. You cannot know intra-bar ordering from OHLC; the contract handles that honestly with the flag.

The envelope only becomes invalid where it is **reused across a grid** — i.e. in `walk_envelopes`, the picker. The live resolver is not wrong. The layer that *chooses* geometry is. That is precisely why it is dangerous: it has been scoring candidate brackets against a representation in which a wider target is structurally cheaper to reach.

---

## 2. The constraint that shapes the fix

The chooser has **OHLC only** (`h`, `l`, `c` per bar) — no tick data. **Intra-bar sequence is unknowable.** Therefore the fix is **not** "recover the true path"; it is to construct a **policy-consistent ordered path** that obeys the same first-passage rule the resolver applies, so a grid search over that path cannot manufacture free TP.

The ordering rule must match `settlement_same_bar_policy` (currently `sl_first`): when a bar's range spans both levels, the **adverse** extreme is assumed to occur **before** the favourable one. This makes the picker conservative in exactly the way the resolver is, and — critically — makes a wide TP no longer free, because reaching it now requires surviving the adverse wick first.

---

## 3. Proposed reconstruction (spec, not built)

Replace the independent `fav`/`adv` with a single direction-signed, monotonically-ordered **path** per signal, from which the grid walk reads first passage.

**Per bar, per signal (direction `d ∈ {+1 buy, −1 sell}`, entry `e`, atr `a`):**

From the bar's `o, h, l, c`, build an ordered sequence of up to 4 points that respects the `sl_first` assumption:

1. **adverse extreme first** (conservative): if `d=+1`, the low `l` is visited before the high `h`; if `d=−1`, the high before the low.
2. Insert `o` at the start and `c` at the end (both are known-real points).
3. Order the four points `o, adverse, favourable, c` by the policy — i.e. the adverse extreme is placed before the favourable extreme within the bar.

This yields a per-bar **ordered** excursion path `p[0..3]` in ATR units: `(point − e) * d / a`. Stacking bars gives an `(n, 48*4)` signed path. `walk_envelopes` then reduces to the existing `walk` (first-passage over a 1-D signed path), which is already path-consistent and shares the resolver's rule.

**Properties this guarantees:**
- A bar can no longer satisfy a wide TP without first passing through its adverse extreme — killing the monotonic-in-tp artifact.
- The `same_bar_policy` is expressed **in the path order**, not as a post-hoc tie-break, so the picker and resolver apply the same convention by construction.
- Expiry is unchanged: a signal that never touches either level within 48 bars still expires.

**Cost:** 4× the path length (192 points/signal vs 48). Acceptable — the grid search is `n × |tp_grid| × |sl_grid|` vectorised numpy, and 192-length arrays are trivial.

---

## 4. Validation gate (must pass before any grid search trusts it)

Do **not** ship the reconstruction on plausibility. Prove it against the live resolver:

1. **Agreement test:** for a sample of era-clean signals, the reconstructed-path first-passage verdict (tp/sl/expired) must match `resolve_signal_outcome_ohlc`'s verdict within a small tolerance. Mismatches should be concentrated exactly in the `ambiguous` (same-bar) cases — if they appear elsewhere, the ordering rule is wrong.
2. **Monotonicity test:** expectancy must be **non-monotonic** in tp across the extended grid (there must be an interior optimum, not a boundary pick). A pick still pinned at `tp_atr_max` after reconstruction means the fix failed.
3. **Discreteness test:** pnl should no longer be strictly `{+tp, −sl, 0}` — a coherent path produces intermediate exit values when a bar's ordered points land between levels. If pnl is still purely discrete, the path is still not being walked as a path.

Only after 1–3 pass does the extended-grid backtest run. This inverts the earlier order (diagnostic → backtest) specifically because the diagnostic was the broken thing.

---

## 5. What this does NOT touch

- **The live resolver / settlement contract** — unchanged and correct. This is a picker-side reconstruction only.
- **`resolve_bracket_from_envelope`** — stays as-is; it is consumed once per signal, where independent `fav`/`adv` is the honest OHLC approximation.
- **`p_win_bracket` / reachability / calibration** — they read stored `outcome` (the resolver's verdict), not the envelope, so they are unaffected and remain era-clean.

---

## 6. Relationship to the AAPL regime-suppression question

Deferred deliberately. Regime/direction suppression (as done for AAPL) is a *mitigation applied once the measurement is trusted*. Here the measurement is broken, so suppressing forex regimes would mask the envelope defect behind a gate. Fix the path first; then the four genuinely-`None` symbols (XAUUSD/XAUAUD/USDJPY/XAUEUR) become a real regime/direction optimization on top of a sound chooser.

---

## 7. Files this will touch (when built)

| File | Change |
|---|---|
| `regime_platform/sweep_orchestrator/tiered_atr.py` | replace independent `fav`/`adv` in `build_envelopes` with a policy-ordered path; `walk_envelopes` delegates to the path-consistent `walk` |
| `tests/test_geometry_basis_harmonization.py` (or new) | the 3 validation gates: resolver-agreement, non-monotonicity, non-discrete pnl |
| `worklog/20261009_ENVELOPE_RECONSTRUCTION.md` | this |

**Open design question for review:** the ordering rule assumes adverse-before-favourable uniformly (the `sl_first` convention). An alternative is to order by the bar's open→close direction (a bar that closed up is assumed to have dipped then risen). The uniform rule is simpler and matches the current policy exactly; the directional rule is more realistic but couples the chooser to a heuristic. Recommendation: **uniform `sl_first`** first (provably conservative, matches the resolver), evaluate the directional variant only if the agreement test shows systematic mismatch on up-close bars.

</details>

---

## 8. The corrected prescription — walk-forward validation, not an envelope rebuild

Since the envelope is faithful, the chooser's picks are *correctly computed* — the open question is whether they **generalize**. Three gates, in order of priority:

1. **Out-of-sample split.** Currently `select_tp_sl`'s only robustness check is the in-sample time split (`e1>0 AND e2>0`). Add a true held-out test: pick the bracket on the first half, *evaluate* it on the second half (and vice versa). A tp=6/sl=1 pick that only earns on the half it was chosen on is overfit; one that earns on both is real. This is the single highest-value check.

2. **Minimum sample per half.** n≈68 total → ~34 per half is too thin to trust a RR=6 structure. Raise `min_signals` (or gate the pick's `n`) so a bracket is only adopted with enough independent episodes in *each* half — the platform's existing calibration min-episode convention (20) is the natural bar.

3. **Regime-diversity.** All current evidence is one trending week. A pick should be required to survive across vol-tiers / trend directions, not just time. If tp=6 only wins in `high_vol`/`med_high_vol`, the pick is regime-conditional and should be scoped as such (this is where the AAPL-style regime/direction suppression *becomes* relevant — after the chooser is trusted, to scope its output, not to hide a defect).

**Relationship to AAPL suppression (revised):** still deferred, but for the *opposite* reason than §6 stated. §6 said "suppress after fixing the broken measurement." The measurement isn't broken — so suppression isn't a mask; it's a legitimate **scoping** tool for gate 3 (restrict a pick to the regimes where it generalizes). It moves from "deferred until sound" to "a real option once gates 1-2 pass."

**Bottom line:** do not rebuild the envelope. Build the walk-forward split + min-sample bar first. If a tp=6/sl=1 pick survives held-out evaluation with n≥20 per half across regimes, it's real and the chooser can adopt it. If it doesn't — which the single-regime, thin-sample evidence suggests is likely — the chooser correctly stays conservative, and that is the honest outcome, not a bug to engineer around.
