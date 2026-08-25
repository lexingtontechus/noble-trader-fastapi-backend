# 2026-08-25 — Part 1 Change B: derive p_markov_hold_n from RegimeHMM (shadow mode)

**Scope:** `regime_platform/core/regime_engine.py` (new
`RegimeHMM.trend_transition_matrix_3state()`),
`regime_platform/sweep_orchestrator/orchestrator.py` (new
`MARKOV_SOURCE_MODE` flag + wiring at the `emit_markov` call site),
`tests/test_regime_stability.py`. Part 1 of
`.claude/plans/goofy-rolling-swing.md`.

**Status:** Built, tested (616 passed, 0 failed — 611 + 5 new), committed
(`e8ef5ca`), pushed to `origin/main`. Deploys in `legacy_hmm` mode
(unchanged behavior) until `MARKOV_SOURCE_MODE=trend_hmm_shadow` is set
on the Studio.

---

## Trigger

After the auth-simplification restart today, the user reported:

```
[WARNING] regime.hmm.model: hmm.emit: XAUUSD — predict failed
(ValueError: startprob_ must sum to 1 (got nan)), using FLAT
```

Investigated live via SSH rather than assuming — this is
`regime_platform/services/hmm/model.py`'s standalone 5-state `HmmModel`
(fit on 9 unscaled features: log_return + 3 microstructure + 5 TA
indicators), **not** `RegimeHMM` (the one Change A already fixed via
feature standardization). Traced the actual sequence in
`logs/orchestrator.log`:

1. `hmm.fit: XAUUSD — state collapse detected (all 5 states → 'FLAT')` —
   an existing guard fires, falls back to a uniform 3×3 matrix.
2. A few ticks later, `hmm.emit: XAUUSD — predict failed (ValueError:
   startprob_ must sum to 1 (got nan)), using FLAT` — the model's
   leftover `startprob_` from the collapsed fit is NaN; `predict()`
   throws, also caught, also falls back to the identical uniform/FLAT
   result.

Both paths land on the same safe output — this was never a live
incident, just noisy logging. But the frequency this boot was broad and
high: 27× XAUAUD, 22× USDCAD, 13× XAUEUR, 10× USDCHF, 9× XAUUSD, plus
BTCUSD/USDJPY/US500/US30USD/GBPUSD/EURUSD. Confirmed via `AskUserQuestion`
that this reinforced the case to build Change B now rather than continue
carrying a known-broken second model — the user agreed.

## Fix

1. **`RegimeHMM.trend_transition_matrix_3state(trend_state)`**
   (`regime_engine.py`) — derives the same 3×3 `{UP,DOWN,FLAT}` matrix
   shape as `HmmModel.emit_markov()`, but from the trend model's own
   `transmat_` (already fitted, already sanitized/finite via
   `_sanitize_model`, already stabilized by Change A's feature
   standardization) — no new fitting, no new numerical instability.
   `{strong_bull,bull}→UP`, `{strong_bear,bear}→DOWN`. FLAT never
   receives a mapped component (the trend model has no neutral state),
   so its row falls through to the uniform-prior fallback — mirroring
   the legacy model's own `_aggregate_transition` "no state maps to this
   label" handling rather than producing an invalid all-zero row.
   `current_state` is derived from the caller's `regime.trend_state`
   (already computed every tick) through the identical grouping, so the
   returned matrix and the returned current-state can never disagree
   with each other.
2. **`MARKOV_SOURCE_MODE`** (`orchestrator.py`, module-level
   `os.getenv` constant — same pattern as the existing `TIMESFM_GATE_MODE`,
   not a `settings.py` field):
   - `"legacy_hmm"` (default) — unchanged behavior, the standalone
     `HmmModel` remains the sole source.
   - `"trend_hmm_shadow"` — computes both, logs a comparison line
     (`%s: markov shadow — legacy current_state=%s row=%s | trend_hmm
     current_state=%s row=%s`), still feeds the **legacy** value to the
     live blend. Zero behavior change — observation only.
   - `"trend_hmm_live"` — feeds the new value to the live blend instead.
   Wired into `_process_symbol` right after the existing
   `self._hmm_model.emit_markov(symbol)` call, wrapped in try/except so
   any derivation error (there shouldn't be any — the trend model is
   already fitted and sanitized by the time this runs) never touches the
   live blend, it just skips the shadow/live substitution for that tick
   and falls back to whatever the legacy model already produced.
3. **Tests** (`tests/test_regime_stability.py`, new section 5): a
   hand-crafted 4×4 `transmat_` + label map (bypassing a real fit) with
   hand-computed expected `T3` values, confirming the aggregation/
   renormalization math independent of what a real EM fit happens to
   produce; FLAT's uniform-fallback row; `current_state`'s grouping
   across all 4 trend labels plus the unmapped-label fail-open-to-FLAT
   case; a pre-fit `RuntimeError` check; and one real-fit integration
   test (synthetic price series, real `hmmlearn` output) confirming the
   wiring end-to-end. **Revert-and-confirm**: flipped the UP/DOWN
   mapping, confirmed 2 of 5 new tests failed on the mutation with the
   exact expected assertion (`'DOWN' == 'UP'`), reverted, confirmed all
   5 pass again.

## Verification

Full suite: **616 passed** (611 + 5 new), **21 skipped, 0 failed**.
`python -c "from regime_platform.sweep_orchestrator.orchestrator import
MARKOV_SOURCE_MODE"` confirms the default is `"legacy_hmm"` — this
commit alone changes nothing about live behavior until the env var is
set.

## Deployment status

Committed as `e8ef5ca`, pushed to `origin/main`. **Not yet running in
shadow mode** — the code is live once the Studio restarts, but
`MARKOV_SOURCE_MODE` defaults to `legacy_hmm` (unchanged). To start the
observation window, set `MARKOV_SOURCE_MODE=trend_hmm_shadow` in the
Studio's environment and restart. Once running, verify via
`grep "markov shadow" logs/orchestrator.log` that the comparison lines
appear and that the legacy value is still the one flowing into
`nt_sweep_result`'s `markov_p_up`/`markov_p_dn` columns (unchanged).

## Not done in this pass (by design, staged per the plan)

- **B3** (historical sanity check — join `nt_sweep_result.markov_p_up/
  markov_p_dn` to `nt_signal_sim.outcome`, confirm the old model's
  implied confidence wasn't actually correlated with realized win/loss)
  — deferred until there's a shadow-mode window to also compare the NEW
  derivation's own historical correlation against, not just build in
  isolation right after the flag ships.
- **B4** (cutover to `trend_hmm_live`) — gated on a 3-7 day shadow window
  showing the new derivation isn't worse than the legacy one.
- **B5** (delete `regime_platform/services/hmm/model.py`,
  `feature_buffer.py`, the `hmm_quote_fetcher` call, `SweepOrchestrator`'s
  `_hmm_buffer`/`_hmm_model` construction, `MARKOV_SOURCE_MODE` itself,
  and the related tests) — only after B4 cutover is confirmed stable.
  `hmm_ta_fetcher`/`latest_ta()`/`nt_regime_log.ta_*` writes are
  explicitly NOT part of this deletion (separate purpose — regime
  analytics, confirmed no other writer) and will need a small adapter if
  `FeatureBuffer` itself is deleted in B5.
