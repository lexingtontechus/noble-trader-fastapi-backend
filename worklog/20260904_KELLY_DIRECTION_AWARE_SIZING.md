# 2026-09-04 — Kelly sizing is direction-blind: scope

## Trigger

Triage of "no qualified signals for forex/commodities"
(`20260904_MIN_RR_REGRESSION.md` covered a separate, already-fixed
cause). Found live: XAGUSD/XAUEUR/XAUUSD are all currently reading
`bear`-family regimes with a real, price-confirmed decline over their
fit window (verified against real price history: XAUUSD -4.35%,
XAUAUD -5.1%, XAUEUR -4.0%, XAGUSD -2.6%) — a genuine short setup — yet
`kelly_f` is bit-for-bit `0.0000` on every single tick, all day. Same
pattern independently confirmed for EURUSD/GBPUSD.

## Root cause

`position_sizer.py::PositionSizer.size()` computes edge as
`mu = mean(returns)`, `kelly_f = clip(mu / variance, 0, 1)` — always
answering "would a LONG position here have had positive edge?" It never
receives the trade's actual `direction`; `PositionRequest` has no such
field, and `_process_symbol` doesn't even compute `direction` until
well after Kelly has already run (`orchestrator.py`, currently at
`direction = "short" if "bear" in regime_label.lower() else "long"`,
~line 2312 — Kelly sizing is ~line 2115-2129).

Net effect: any symbol in a genuine, real downtrend gets `kelly_f=0`
regardless of whether the intended trade is long (correctly zero — no
edge) or short (incorrectly zero — the decline IS the edge for a
short). `_apply_regime_kelly_floor` (fix #3b, 2026-08-22 audit)
independently already explicitly withholds its floor when `mu < 0`
("a negative historical edge is not force-sized just because regime
confidence is high") — correct reasoning, but evaluated on the same
direction-blind `mu`, so it inherits the same blindness for shorts.

## Fix

**`position_sizer.py`**:
- `PositionRequest` gains `direction: str = "long"`.
- `size()`: immediately after building the returns array
  (`r = np.array(req.returns, dtype=float)`), negate it when
  `req.direction == "short"` (`r = -r`), before anything else is
  computed from it. Variance/sigma are unaffected by a sign flip
  (squared), so only `mu`, `annual_mu`, `sharpe`, `kelly_edge`, and
  `full_kelly` (all downstream of `mu`) correctly flip to reflect the
  short's edge instead of the long's. No other line in `size()` needs
  to change — this one negation is sufficient because every quantity
  downstream is already a function of `mu`/`sigma2` computed from `r`.
- Default `direction="long"` — any existing caller that doesn't pass
  it (there are none outside `_process_symbol` today, but this keeps
  the dataclass regression-safe) behaves byte-identically to today.

**`orchestrator.py`**:
- Add a SECOND, earlier direction read — `_kelly_direction`, computed
  right after `regime_label`/`regime_conf`/`trend_probs` are set, from
  the raw label — for Kelly sizing/floor only. NOT a straight move of
  the existing `direction` line: that one is deliberately positioned
  AFTER the regime-label-debounce block ("2c"), so it can pick up a
  debounced label once `regime_label_debounce_mode=live` (per that
  block's own comment — "direction selection" was explicitly named as
  one of the two consumers meant to see the debounced value). Moving
  it earlier would have silently undone that design the moment this
  ships, even though `regime_label_debounce_mode` is `shadow` today so
  nothing observable would break immediately. Two names, two purposes:
  `_kelly_direction` (raw, Kelly-only, computed early) and `direction`
  (final, computed at its original post-debounce location, used for
  the actual trade signal/EV blend/persistence) — same "Kelly floor
  stays on raw" boundary Fix 2 already drew, just made explicit for
  Kelly sizing too rather than silently inherited.
- Pass `direction=_kelly_direction` into the `PositionRequest(...)`
  call.
- **Separately**: `_mu` (the diagnostic mean computed independently at
  ~line 2132, `_mu = float(np.mean(np.array(returns, dtype=float)))`)
  is NOT automatically fixed by the `position_sizer.py` change — it's
  a second, orchestrator-level copy of the same calculation, used only
  for the "kelly math" log line and passed directly into
  `_apply_regime_kelly_floor`. Fixing the sizer alone would leave the
  floor still evaluating the raw, direction-blind `mu`. Compute
  `_mu_for_floor = _mu if direction == "long" else -_mu` and pass THAT
  into `_apply_regime_kelly_floor` instead of raw `_mu` — the floor
  function itself (`_apply_regime_kelly_floor`, pure, already tested in
  `tests/test_kelly_regime_floor.py`) needs no code change, only a
  different value handed to its existing `mu` parameter.
- Log lines (kelly-math, floor-withheld/applied) get `direction` added
  for readability — a value flipping sign silently in a log line is
  exactly the kind of thing that causes confusion later.

## Explicitly not touched

- `ev_engine.py`/`compute_ev`/`p_win` and its blend inputs
  (`p_regime`/`p_markov`/`p_timesfm`/`p_historical`) — completely
  separate code path from Kelly sizing; `kelly_min_ok` is one
  independent qualification gate among several, ANDed with
  `ev_allowed`/`p_win_ok`, not an input to them. This fix cannot change
  whether a signal passes those other gates, only whether it passes
  `kelly_min_ok` and what `kelly_f`/`effective_kelly` (the actual
  position size) end up being.
- `_apply_regime_kelly_floor`'s own logic (unchanged, still pure,
  existing tests remain valid as-is — only the value orchestrator.py
  passes into its `mu` parameter changes).
- No schema/column changes — `direction` doesn't need to be persisted
  anywhere new; it's already derivable from the persisted `regime`
  column after the fact if ever needed.
- `heavy_sweep.py`'s own Kelly-adjacent sizing path (not audited here —
  out of scope for this pass; flag as a possible follow-up if the same
  pattern exists there).

## Impact, precisely

- Any tick where `direction == "long"`: zero change, `mu` untouched.
- Any tick where `direction == "short"` and raw `mu < 0` (price
  genuinely declining, aligned with the short): `kelly_f` goes from
  incorrectly `0` to correctly reflecting the short's edge — this is
  the intended fix, and is what's currently happening for
  XAGUSD/XAUEUR/XAUUSD/EURUSD/GBPUSD.
- Any tick where `direction == "short"` and raw `mu > 0` (regime says
  bear but recent price action hasn't confirmed it yet — e.g. XAUAUD's
  current state, `bull` regime label with negative mu, is the mirror
  case and stays correctly zero either way): the OLD code would have
  handed this a nonzero "long-shaped" Kelly number even though the
  trade about to be placed is a short — arguably already wrong today.
  The fix corrects this too (negated positive mu -> negative -> zero).
- No change to qualification volume beyond the `kelly_min_ok` gate —
  EV/p_win gates are untouched, so this can only let through signals
  the rest of the pipeline already independently likes.

## Tests (new)

`tests/test_position_sizer_direction.py`:
- Default `direction="long"` behaves byte-identically to omitting the
  field entirely (regression safety).
- `direction="short"` with negative returns produces the SAME
  `full_kelly_f`/`sharpe_ratio` magnitude as a `direction="long"` call
  on the sign-flipped (positive) version of those same returns —
  proves the negation is the only thing happening, not a parallel
  formula.
- `direction="short"` with positive returns (misaligned case) still
  clips to `kelly_f=0`.
- `direction="long"` with positive/negative returns unchanged from
  today's existing behavior (no regression on the long path).

`tests/test_kelly_regime_floor.py`: unchanged — `_apply_regime_kelly_floor`
itself isn't modified, only the value the orchestrator now passes into
its `mu` parameter changes, which isn't a concern of that pure
function's own unit tests.

## Build notes (2026-09-04, same day)

Built exactly as scoped, with one correction found mid-build (see
below). New tests: `tests/test_position_sizer_direction.py` (6) —
default-long regression safety, long-on-decline still zero, short-on-
decline now positive, short-on-rally (misaligned) still zero,
short-on-decline mirrors long-on-negated-returns exactly (proves it's a
pure sign flip, not a parallel formula), variance/vol-scaling unaffected
by direction. `tests/test_kelly_regime_floor.py` untouched, still
passes — that pure function wasn't modified. Full suite: 694 passed
(up from 688), 21 skipped, 0 failed.

**Correction found while wiring `orchestrator.py`**: the original plan
above ("move the existing `direction` line up") would have silently
broken Fix 2's own documented design — the debounce block's comment
explicitly names "direction selection" as one of the two consumers
meant to see the debounced label once `regime_label_debounce_mode=live`.
Moving `direction`'s computation before that block would have locked
Kelly AND the eventual trade signal onto the raw label permanently,
undoing that. Fixed by keeping two reads: `_kelly_direction` (new,
early, raw-label-based, Kelly-only) and `direction` (unchanged position
and behavior, still computed after the debounce block, still feeds the
trade signal/EV blend/persistence). No current behavior difference
either way since debounce mode is still `shadow`, but this avoids
quietly re-breaking Fix 2's intent the moment it ever goes live.

**Verified against live market data**, not just unit tests: recomputed
Kelly for 6 symbols using real current price fetches, comparing the old
(always-long) formula against the new direction-aware one:

| Symbol | regime | direction | old kelly | new kelly |
|---|---|---|---|---|
| XAUEUR | high_vol_strong_bear | short | 0.0000 | **1.0000** |
| XAUUSD | high_vol_strong_bear | short | 0.0000 | **1.0000** |
| XAGUSD | med_low_vol_strong_bear | short | 0.0000 | **1.0000** |
| XAUAUD | med_low_vol_bull | long | 0.0000 | 0.0000 |
| EURUSD | high_vol_bull | long | 0.0000 | 0.0000 |
| GBPUSD | low_vol_strong_bull | long | 0.0000 | 0.0000 |

Exactly the scoped behavior: the three symbols currently in a genuine,
real bear regime go from a completely wasted `0` to a real, strong
edge; the three currently reading bull (EURUSD/GBPUSD had flipped back
to bull by the time of this check, market conditions moving in
real time) show no change at all, confirming the fix is inert wherever
it isn't supposed to fire. `recommended_f` hitting the clipped max
(1.0) reflects how smooth/low-variance the metals' current decline has
been (mu/sigma2 is large) — a property of the real data, not of this
fix; the existing fractional-Kelly/vol-scaling/regime-gating layers
downstream are unchanged and still apply on top in production.
