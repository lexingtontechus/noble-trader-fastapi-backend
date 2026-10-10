# Walk-forward split for the geometry chooser — validating picks out-of-sample

**Date:** 2026-10-09
**Scope:** `tiered_atr` — add a held-out (walk-forward) validation gate so the dynamic-ATR chooser cannot adopt a bracket that only earns on the half of the sample it was chosen on. Follows `20261009_ENVELOPE_RECONSTRUCTION.md` §8, which overturned the envelope-rebuild and identified **thin-sample / single-regime expectancy** as the real false-signal risk.
**Status:** **Built**, shadow-first (`dynamic_atr_walkforward_mode: shadow`). Pure function + wiring + tests. No live behaviour change until flipped.

---

## 1. Why this and not the envelope rebuild

`20261009_ENVELOPE_RECONSTRUCTION.md` §0 proved `walk_envelopes` and `resolve_bracket_path` agree to the last decimal — the envelope is faithful, and the monotonic-in-tp signature is **real data** (XAGUSD P(tp) only falls 0.76→0.68 as tp goes 1.5→6.0, so tp=6/sl=1 is genuinely optimal on that sample).

The false-signal risk is therefore **not** a broken walk. It is that `select_tp_sl`'s only robustness check is an **in-sample time split** (`e1>0 AND e2>0` on the *same* data the bracket was chosen from). A RR=6 pick chosen *because* it maximises expectancy on N signals will, by construction, tend to look stable on that same N — the split shares the selection. The missing check is a **true held-out evaluation**: choose the bracket on one half, measure it on the other.

## 2. The defect, precisely

`select_tp_sl` (tiered_atr.py:459-461):

```python
e1, e2 = float(net[:mid].mean()), float(net[mid:].mean())
if e1 <= 0 or e2 <= 0:
    continue
```

Both halves are drawn from the **same envelope the grid is being optimised over**. The bracket is picked to maximise `net.mean()`; requiring both sub-means positive is a weak, selection-coupled sanity check, not out-of-sample evidence. For n≈68 (≈34/half) across one trending week, a tp=6/sl=1 structure passes trivially while carrying no proof it generalises.

## 3. What was built

### 3.1 `walk_forward_validate` (pure, in `tiered_atr.py`)

```python
def walk_forward_validate(env, tp_atr, sl_atr, *, same_bar_policy, cost_atr,
                          min_fold_n, min_oos_expectancy) -> dict
```

- Splits the **chronologically-ordered** envelope in half (`build_envelopes` sorts by `_ts`, and `_load_class_signals` is `order: ts.asc`, so row order is time order).
- **Fold A:** choose nothing — just measure the *given* bracket's expectancy on the first half (`is`).
- **Fold B:** measure the same bracket on the second half (`oos`).
- Returns `{is_exp, oos_exp, is_n, oos_n, ok, reason}`.
- `ok` requires: each fold has ≥ `min_fold_n` decided signals **and** **both** folds' expectancy ≥ `min_oos_expectancy` (net of cost). The pick was chosen on the *full* sample, so both halves are partly in-sample; the held-out test is that it does not collapse on **either** half. A pick that earns only because one half carried it is the overfit signature (caught in either direction — first-half-collapse → `is_expectancy_below_floor`, second-half → `oos_expectancy_below_floor`). Fail-open (`ok=True`, `reason="insufficient_data"`) when either fold is too thin — a thin sample must not silently kill a pick, it defers to the existing behaviour.

The tests caught the asymmetry during the build: the first version checked only the out-of-sample fold, which would pass a pick whose *first* half collapsed. Corrected to check both. `dynamic_atr_pick` calls it with the full envelope and the *chosen* pick, so it validates the actual selection.

### 3.2 Wiring in `dynamic_atr_pick`

After `select_tp_sl` returns a pick (and after the Stage-5 consistency gate), before `write_pick_to_store`:

```python
if pick is not None and wf_mode != "off":
    wf = walk_forward_validate(env, pick.tp_atr, pick.sl_atr, ...)
    if wf_mode == "shadow":
        logger.info("[dynamic:atr] %s walk-forward %s (is=%+.3f oos=%+.3f)",
                    symbol, "ok" if wf["ok"] else "WOULD-REJECT", ...)
    elif not wf["ok"]:      # live
        return None          # reject the overfit pick, heavy kept
```

- **shadow:** log the is/oos expectancy and whether it *would* reject. No behaviour change.
- **live:** a pick whose held-out fold is negative (or too thin is NOT rejected — fail-open) is dropped; the heavy-sweep geometry is kept.
- **off:** gate skipped entirely.

### 3.3 Flag `dynamic_atr_walkforward_mode` (off | shadow | **shadow**)

Ships at `shadow`, consistent with every other new lever. Config knobs: `dynamic_atr_walkforward_min_fold_n` (default 20 — the platform's calibration min-episode convention) and `dynamic_atr_walkforward_min_oos` (default 0.0 — held-out expectancy must be non-negative net of cost).

## 4. What this catches (the intended effect)

- A tp=6/sl=1 pick that earns +3.7 on the first half and **−1.2** on the second → `ok=False` → rejected in live mode. That is the overfit signature the in-sample split misses.
- A pick that earns on **both** halves → passes, and now carries actual out-of-sample evidence rather than a selection-coupled sanity check.
- Thin folds (n<20) → fail-open, deferred to prior behaviour (not silently rejected).

## 5. Files changed

| File | Change |
|---|---|
| `regime_platform/sweep_orchestrator/tiered_atr.py` | `walk_forward_validate` (pure) + gate in `dynamic_atr_pick` |
| `regime_platform/core/feature_flags.py` | `get_dynamic_atr_walkforward_mode` + `min_fold_n` + `min_oos` getters/defaults |
| `config/feature_flags.yaml` | `dynamic_atr_walkforward_mode: "shadow"` + knobs |
| `tests/test_atr_walkforward.py` | **new** — overfit-reject (both directions), both-halves-pass, thin-fold fail-open, configurable floor |
| `worklog/20261009_WALKFORWARD_SPLIT.md` | this |

Full suite: **1906 passed, 21 skipped, 1 pre-existing failure** (`test_yaml_ships_shadow_qs` — unrelated).

## 6. Not in scope (deferred, per §8 of the envelope worklog)

- **Regime-diversity gate** — requires the pick to survive across vol-tiers, not just time. This is where AAPL-style regime/direction *scoping* becomes relevant, but only after the walk-forward gate proves the pick generalises at all. Separate build.
- **Raising `min_signals` globally** — the fold-level `min_fold_n` is the targeted version; a global raise is a broader tuning decision.
