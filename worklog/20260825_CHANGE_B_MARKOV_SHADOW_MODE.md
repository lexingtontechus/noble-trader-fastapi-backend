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

## B3 — Historical sanity check (2026-08-25, follow-up)

Built the join `nt_sweep_result.markov_p_up/markov_p_dn` (107,627 rows
with non-null markov data, 2026-05-31 through today) → nearest-timestamp
`nt_signal_sim.outcome` (restricted to `tp_hit`/`sl_hit` only — `expired`
excluded as neither a win nor a loss), same symbol, tolerance ±2 min
(actual matches landed at ~0.5s average — the two tables are written
moments apart in the same tick, so the join is effectively exact, not
approximate).

**Per-signal-direction breakdown** (22,838 matched rows with a clean
tp/sl outcome):

| signal | outcome | n | avg markov_p_up | avg markov_p_dn |
|---|---|---|---|---|
| buy | tp_hit | 8,440 | 0.0786 | 0.0112 |
| buy | sl_hit | 3,753 | 0.1209 | 0.0704 |
| sell | tp_hit | 3,238 | 0.0582 | 0.0668 |
| sell | sl_hit | 7,407 | 0.1107 | 0.0423 |

**Single-number check**: defined `directional_edge` = `p_up - p_dn` for
buy signals, `p_dn - p_up` for sell signals (i.e. "how much probability
mass did the legacy model put on the direction that was actually taken,
net of the opposing direction"), and `win` = 1 for `tp_hit`, 0 for
`sl_hit`.

- Pearson correlation between `directional_edge` and `win`: **r = 0.1467**
  (n = 22,838 — large enough that this is not noise, but the effect size
  itself is small).
- Mean edge on wins: **+0.0511**. Mean edge on losses: **-0.0284**. The
  relationship points the right way (winners had, on average, more of the
  model's probability mass on the correct side) but it's a weak signal —
  most of the model's mass sits on FLAT (both p_up and p_dn are under
  0.13 in every bucket above), consistent with the state-collapse/NaN
  instability already confirmed live.

**Conclusion**: the legacy model is not pure noise and not actively
inverted — a real, small, statistically-detectable positive correlation
exists (r≈0.15). This sets the bar B4 needs to clear: the new
`trend_hmm`-derived value doesn't need to be excellent, it needs to not
be worse than r≈0.15 on the same kind of check. **This check can only
characterize the OLD model** — it says nothing about how the NEW
derivation performs, because the new derivation has never run against a
single live tick. See "Cutover timing" below.

## B4 — Cutover timing (surfaced 2026-08-25, resolved: shadow first)

The user's instruction was "build B3 and let's cutover." B3 (above) can
only characterize the OLD model — it can't validate a derivation that's
never executed. Surfaced this back to the user via `AskUserQuestion`
rather than resolving it unilaterally; user chose "shadow first, then
cutover."

**Live check at decision time turned up a correction**: `MARKOV_SOURCE_
MODE` is not sitting at its default — it's `trend_hmm_shadow` on the
actual running orchestrator process (confirmed via `/proc/<pid>/environ`
on the Studio), evidently set by the user as a Studio secret at the last
restart (2026-08-25 20:46 UTC), following the `.env.example` checklist
added earlier today. Shadow mode has been live and running since then.

`grep "markov shadow" logs/orchestrator.log`: 98 comparison lines
20:50:03–21:21:19 UTC, zero `trend_hmm markov derivation failed`
errors, matrices are finite/sane (no NaN, no collapse) for every symbol
sampled. Only ~35 minutes of data at the time of this check — far short
of the plan's 3-7 day window. No cutover yet; letting it accumulate real
time before revisiting `MARKOV_SOURCE_MODE=trend_hmm_live`.

**Unrelated finding from the same live check, flagged immediately**: the
`/proc/<pid>/environ` read also printed the live `API_KEY` value in
plain text into the agent session transcript. Recommended rotating that
key since it's now been displayed outside the Studio secrets store.

## Not done in this pass (by design, staged per the plan)

- **B4** (cutover to `trend_hmm_live`) — timing decision above.
- **B5** (delete `regime_platform/services/hmm/model.py`,
  `feature_buffer.py`, the `hmm_quote_fetcher` call, `SweepOrchestrator`'s
  `_hmm_buffer`/`_hmm_model` construction, `MARKOV_SOURCE_MODE` itself,
  and the related tests) — only after B4 cutover is confirmed stable.
  `hmm_ta_fetcher`/`latest_ta()`/`nt_regime_log.ta_*` writes are
  explicitly NOT part of this deletion (separate purpose — regime
  analytics, confirmed no other writer) and will need a small adapter if
  `FeatureBuffer` itself is deleted in B5.
