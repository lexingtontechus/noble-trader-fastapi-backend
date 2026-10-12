# ev_allowed inverted selection — gate reads directional EV, not bracket EV

**Date:** 2026-10-12
**Scope:** Trace the published-book (6.2% WR) vs suppressed-book (12.6% WR) inversion to its gate and fix it. Proven: `ev_allowed` gates on the **directional** EV while the **bracket** (reachability) EV — the honest one — is computed but never reaches the gate. One-line object-aliasing bug.
**Status:** Root-caused with data; fix built, E2E + replay, deployed.
**Roadmap:** geometry harmony / qualification integrity.

---

## 1. The inversion, proven (era-clean, ts≥2026-10-02)

Per-gate fail-WR vs pass-WR (a gate is INVERTING if the signals it rejects win MORE often than the ones it passes):

| gate | fail_n | fail_WR | pass_WR | verdict |
|---|---|---|---|---|
| **ev_allowed** | 2,558 | **14.9%** | **8.1%** | **strongly inverted** |
| p_win_ok | 727 | 14.0% | 9.8% | inverted |
| regime_conf_ok | 3,086 | 10.9% | 9.7% | mildly inverted |

`ev_allowed` rejects signals that win 14.9% and passes signals that win 8.1% — suppressing ~2,558 winners at ~2× the rate of the losers it lets through. Within the same bucket (2-4: ev-fail 19.8% vs ev-pass 16.2%; 1-2: 10.9% vs 6.1%), so it's selection, not composition.

## 2. Root cause — object aliasing (orchestrator.py)

The chain:
1. `compute_ev()` → `ev_result` with directional `p_win`/`ev`/`allowed`.
2. TimesFM soft-mode may create `adjusted_ev_result` (a **new** `EVFullResult`).
3. Line 4430: `effective_ev_result = adjusted_ev_result`.
4. Line 4467: `apply_bracket_to_ev(ev_result, ...)` — mutates **`ev_result`** in place, recomputing `ev`/`allowed` from the **bracket** (reachability) probability with expiry.
5. Line 4857: `"ev_allowed": effective_ev_result.allowed` — reads **`effective_ev_result`**, NOT `ev_result`.

**When soft-mode ran, `effective_ev_result is not ev_result`** (two objects). The bracket substitution updates `ev_result.allowed`, but the gate reads `effective_ev_result.allowed` — which still holds the **directional** value. So `ev_allowed` gates on the overconfident directional EV, which is anti-correlated with outcomes (proven: +0.48 bias). The honest bracket EV is computed, stamped, and then **bypassed by the gate**.

Confirmed by data: ev-failed signals have `p_win_bracket` scored 99.5% (substitution ran) yet avg directional-looking ev −4.53; the gate rejected them on the directional number.

## 3. The fix

After `apply_bracket_to_ev` succeeds, the bracket-substituted `ev_result` is authoritative — sync the gate's source:

```python
if _bracket_applied_to_ev:
    effective_ev_result = ev_result   # gate reads the bracket EV, not the stale directional one
```

One line, at the end of the R2 block (after line 4488). `ev_allowed` then reads the reachability-conditioned EV (expire not a loss), which is the honest number. Fail-open preserved: when the bracket is unscored, `apply_bracket_to_ev` returns False, `effective_ev_result` stays directional — the legacy path.

## 4. What this should do

The published book should flip from selecting losers to selecting winners: `ev_allowed` will pass signals whose **reachability** EV is positive (the 14.9%-WR wide-bucket signals it currently rejects) and reject those whose reachability EV is negative. Published WR should rise above the suppressed WR.

## 5. E2E + replay

`tests/test_ev_allowed_reads_bracket.py`: pins the aliasing contract — when `apply_bracket_to_ev` succeeds, `effective_ev_result` is the mutated `ev_result` (gate reads bracket `allowed`); when it fails-open, `effective_ev_result` stays directional. Replay: the era-clean ev-fail cohort (14.9% WR, bracket scored) would now pass the gate on bracket EV.

## 6. Files

- `regime_platform/sweep_orchestrator/orchestrator.py` — sync `effective_ev_result = ev_result` after bracket substitution.
- `tests/test_ev_allowed_reads_bracket.py` — E2E.
- `worklog/20261012_EV_ALLOWED_INVERTED_SELECTION.md` — this.
