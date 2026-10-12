# G2 + G3 — the geometry solve always returns geometry, and the FOREX band replay

**Date:** 2026-10-12
**Scope:** `docs/scopes/20261012_GEOMETRY_COHERENT_FLOW_REDESIGN.md` §3.2 (G2), §3.3 (G3)
**Commit:** (this change)

## Why

The reachability diagnosis (`worklog/20261012_REACHABILITY_GEOMETRY_FAILURE_DIAGNOSIS.md`)
found geometry failing at three points, the first being that **the ATR pick never
reaches the signal**: 9 of 14 symbols ship the brick fallback because
`dynamic_atr_pick` returned `None` and the light path's contract was
"no pick → leave the brick bracket alone". Combined with the eight independent
gates the pick could be declined at, that is a lot of surface for the live book
to silently trade brick geometry.

The redesign's answer, in the user's words: *"if ever a value is none/null —
recompute over fallback (old params!)"*, and *"I do not want bypasses and
patches. I want a contiguous and coherent flow."*

## G2 — the solve always returns geometry

`tiered_atr.select_tp_sl` now tracks two candidates while it walks the grid:

* `best` — clears the **strict** bar (positive expectancy in *both* halves and
  expiry within budget). Unchanged semantics.
* `best_effort` — the **in-band** bracket with the highest net expectancy,
  ignoring that bar.

It returns `best` when one exists; otherwise `best_effort` (G2). `None` is
returned only when `allow_best_effort=False`, which exists purely so the old
strict contract stays reachable (parity replays, tests).

`dynamic_atr_pick` gained the same flag and now **never returns `None` while a
band is configured**. Every former `return None` became:

| former decline | now |
|---|---|
| no active symbols for the scope | `_band_center_pick(cfg)` |
| signals < `min_signals` (thin window) | `_band_center_pick(cfg)` |
| `vol_ratio` outside the configured band | `_band_center_pick(cfg)` |
| consistency gate rejects the walked pick | `_band_center_pick(cfg)` |
| walk-forward gate rejects the walked pick | `_band_center_pick(cfg)` |
| `select_tp_sl` finds nothing | `best_effort` in-band bracket |

`_band_center_pick` is the operator's deliberate mid-band bracket (`n=0`, zero
expectancy — it claims no evidence). That is the point: the last resort is a
**current, in-band, operator-set** value, never a stored/stale/brick parameter.
The gates did not disappear — they still run, log, and change *which* geometry
comes back; they just no longer decide between "geometry" and "no geometry".

## G3 — the light path resolves through the one solve

`orchestrator.SweepOrchestrator._process_symbol` previously did
`read_pick_from_store(symbol, max_age_hours=6.0)` and left the brick bracket in
place on a miss ("Fail-open: cold store / no pick → brick geometry untouched").

It now calls `dynamic_atr_pick(...)`, which is *already* resolve-else-recompute:
it returns the fresh persisted pick when one exists and otherwise recomputes from
current data. With G2 behind it, a miss can no longer leave brick geometry, and
there is exactly one resolution path instead of a store read plus a brick
fallback. The result is cached 6 h per (scope, band), so this is not a per-tick
walk.

## The FOREX band replay — `by_class.forex` (wide) vs `by_symbol.FOREX` (tight)

The Studio's `by_class.forex` held `tp [4.27, 4.52, 4.77] / sl [2.63, 2.75, 2.87]`
(the reopt's wide proposal) while the repo held `tp [1.65, 1.71, 1.75, 1.80] /
sl [0.30, 0.35, 0.40]` (tight). Same forex pooled envelope (1,170 rows,
2026-09-30 → 2026-10-10, split mid=585), three grids, one evidence load:

| band | best cell | e1 (in-sample half) | e2 (other half) | e_mean |
|---|---|---|---|---|
| `by_symbol.FOREX` (tight, git) | 1.65/0.30 | −0.330 | −0.323 | **−0.327** |
| `by_class.forex` (wide, studio) | 4.52/2.75 | **+0.307** | −0.149 | **+0.079** |
| `GLOBAL` (what the pooled solve uses now) | 8.0/2.0 | +0.591 | −0.521 | +0.035 |

**Every one of the 12 tight-band candidates is negative in both halves**
(−0.32 to −0.38). The wide band is the only band with a **positive overall
expectancy** (+0.079) and a **positive first half** (+0.307).

Verdict: **the replay justifies widening.** `by_symbol.FOREX` is the binding
constraint on the whole FX book — the tight stop is tripped by ordinary noise,
which matches the independent ATR-verification finding that the no-edge is
substantially entry-bar range rather than a directional absence of edge.

Caveat, stated plainly: **no forex band clears the strict both-halves bar.**
The wide band is a *regime split* (e1 > 0, e2 < 0) — better, not profitable.
`select_tp_sl` therefore still returns `None` under the strict contract for every
forex cell; only G2's best-effort pass produces a bracket. Widening improves
*which* bracket the book trades (4.52/2.75 at +0.08 instead of 1.80/0.30 at
−0.33); it does not make FX an edge.

**Not applied.** Changing a live band is a trading decision and the evidence here
is "least bad", not "good" — it needs a human go. The change is one edit to
`config/feature_flags.yaml` → `dynamic_atr.by_symbol.FOREX`.

## What "dirty" means for the Studio config

`git status` reporting `M config/feature_flags.yaml` is a **git working-tree
state**, not a runtime one. It is neither "harmonized with the deployment" nor
"dynamically generated" — it is a third state: **hand/reopt-modified on the
Studio and uncommitted**. The 91-line residue is exactly the reopt-solved
`by_symbol` bands (XAGUSD 6.39/2.69, XAUAUD 6.57/4.31, XAUEUR 5.52/3.15,
US30USD 10.99/5.75 …) written by the since-removed `apply_symbol_clamps`. That
same mechanism is what put the wide `by_class.forex` on the Studio and left the
tight one in git. Removing the writer stops *future* drift; it does not
retroactively reconcile bands that already diverged.

## Verification

* `tests/test_geometry_always_returns.py` — 9 tests: best-effort picks the
  highest-expectancy in-band bracket, respects the clamps, is switchable off for
  parity, the strict winner still wins, `_band_center_pick` is the mid-band
  bracket and `None` only when the band is unconfigured, `dynamic_atr_pick`
  returns the band centre instead of `None`, and the G3 wiring assertion.
* Three `tests/test_tiered_atr.py::TestSelect` cases that pinned the old
  `None` contract now assert **both** contracts (strict → `None`,
  default → a flagged bracket); `test_geometry_basis_harmonization`'s
  wick-rejection test keeps its intent via `allow_best_effort=False`.
* Full suite: see the commit. `ruff` introduces **no new** findings on any
  touched file (HEAD/now counts compared per file).
