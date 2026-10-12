# HMM startprob_ NaN on load — silent FLAT regime fallback

**Date:** 2026-10-12
**Scope:** `emit_markov` predict fails with `startprob_ must sum to 1 (got nan)` 18×/24h across 5 symbols (BTCUSD 9, AUDUSD 4, XAGUSD 2, EURUSD 2, USDCHF 1), each silently forcing `current_state="FLAT"`. A FLAT regime cascades: direction defaults, `p_markov` flat, EV blend degraded. Root cause: the persistence round-trip (`save`/`load`) assigns `startprob_` (and the other arrays) raw, with no NaN guard.
**Status:** Root-caused; fix built, E2E, deployed.
**Roadmap:** HMM robustness.

---

## 1. Symptom (live, 2026-10-12)

```
[WARNING] hmm.emit: AUDUSD — predict failed
  (ValueError: startprob_ must sum to 1 (got nan)), using FLAT
```

18 occurrences/24h. `emit_markov` catches the exception and returns `current_state="FLAT"` — so the tick proceeds with a FLAT regime instead of the model's read. FLAT means: no directional bias, `p_markov_hold_n` flat, the EV blend leans entirely on regime/timesfm. For BTCUSD (9 failures) this is frequent enough to materially degrade the signal.

## 2. Root cause — persistence round-trip (model.py)

`fit()` sanitizes the feature matrix with `nan_to_num` (line 175), but the **persisted** model arrays are re-assigned raw on `load()`:

```python
model.means_    = d["means_"]
model.covars_   = d["covars_"]
model.transmat_ = d["transmat_"]
model.startprob_ = d["startprob_"]   # <-- no NaN guard, no renormalize
```

hmmlearn's `predict` validates `startprob_.sum() == 1`. If the fit produced a NaN `startprob_` (a degenerate covariance/mean on a low-variance feature column can do it), `save` persists it verbatim and `load` restores it verbatim → every `predict` throws → FLAT. The crash-recovery pickle is the vector: a model that fitted with a NaN startprob poisons every subsequent process that loads it.

## 3. The fix

Sanitize on **load** (defensive — the pickle may predate the fix) AND on **save** (so a clean model is persisted):

```python
def _clean_startprob(sp):
    sp = np.nan_to_num(np.asarray(sp, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    sp = np.clip(sp, 0.0, None)
    s = sp.sum()
    return sp / s if s > 0 else np.full(len(sp), 1.0 / len(sp))
```

Apply to `startprob_` in both `save` and `load`. `transmat_` rows are also normalized defensively (same NaN risk). `means_`/`covars_` get `nan_to_num` (no renormalize — they're not probabilities). Fail-open: if a model still can't predict after cleaning, `emit_markov`'s existing try/except returns FLAT — unchanged.

## 4. E2E

`tests/test_hmm_startprob_sanitize.py`: a NaN/negative/unnormalized `startprob_` round-trips through save→load and `predict` no longer throws; a clean model is unchanged; all-NaN falls back to uniform.

## 5. Files

- `regime_platform/services/hmm/model.py` — `_clean_startprob` + apply in save/load.
- `tests/test_hmm_startprob_sanitize.py` — E2E.
- `worklog/20261012_HMM_STARTPROB_NAN.md` — this.
