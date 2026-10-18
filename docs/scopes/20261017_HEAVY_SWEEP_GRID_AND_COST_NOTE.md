# Heavy-sweep grid growth, `min_rr`, and the processing-time increase

**Date:** 2026-10-17
**Type:** Evidence note (measurement + config alignment). No behaviour change.
**Trigger:** operator reported the heavy sweep "usually completes < 60 s", contradicting a
~10-minute figure in `20261016_SCALE_ARCHITECTURE_AND_GPU_DECISION.md`. Both were
investigated; the operator was right and the earlier figure was stale.
**Companion:** `20261016_SCALE_ARCHITECTURE_AND_GPU_DECISION.md` §7–§13.

---

## 1. Cadence first — this is a daily job plus intraday reopt urgency

**Do not quote a single duration for the heavy sweep.** It is a **daily** job, and the
reopt runs on **intraday urgency intervals** — it is not one fixed-length run, and every
bare duration that has circulated was a snapshot of one window under one flag set:

| job | cadence | source |
|---|---|---|
| heavy sweep | **daily** | `nt_symbol.sweep_cron` (was weekly → daily, roadmap id=25) |
| `atr_reopt_urgency` | **every 4 h** | vol-urgency nudge on the pre-sweep grid |
| `atr_monthly_reopt` | 1st of month 03:30 UTC | proposal only, never writes YAML |

The per-run duration is a **function of the live flag set** (§2), so it changes whenever
a flag flips. Report the cadence and the config dependence — never a bare number.

## 1b. The August log snapshot (dated evidence, not a current claim)

`data/studio_logs_backup/orchestrator.log` (2026-08-13 → 08-18) carries **25 heavy-sweep
runs**, clustered on a >5 min gap between `DONE` lines. At that time the flags were
`combos=30` and `min_rr=2.0`:

| metric | measured (Aug 2026) |
|---|---:|
| median wall time per run | 75 s |
| min / max | 48 s / 185 s |
| runs under 120 s | 24 of 25 |
| per-symbol `DONE in` — median | 12.7 s |
| `combos` tested per symbol | **30** |

**This is a historical snapshot.** It is not the current cost — the grid has since grown
2.4× (§2) and four more mechanisms have gone live (§4). Cite it only as evidence of what
the job looked like under those flags.

---

## 2. The grid grew 30 → 72 combos

`_renko_grid_search_sync` (`heavy_sweep.py:2096`) iterates
`brick_sizes × SWEEP_SL_RANGE × SWEEP_TP_RANGE`, pre-filtered by `MIN_RR`.

```
SWEEP_SL_RANGE = [3, 5, 7]                    # heavy_sweep.py:123
SWEEP_TP_RANGE = [4, 5, 6, 8, 10, 12]        # heavy_sweep.py:124
```

**Brick count.** `generate_brick_sizes` returns **5** candidates
(`heavy_sweep.py:224`, multipliers `[0.75, 1.0, 1.5, 2.0, 3.0]`).
`generate_atr_anchored_brick_sizes` (`:256`) adds **3** more
(`[0.15, 0.3, 0.6]` × ATR) when `heavy_sweep_atr_anchored_bricks_mode` is live.
That flag is **`live`** (`config/feature_flags.yaml:842`, roadmap id=5 / id=66,
resolved 2026-10-10) → **8 brick candidates**.

**Combos per brick** depends on `MIN_RR`:

| `MIN_RR` | sl=3 (tp≥MIN_RR·3) | sl=5 | sl=7 | per brick |
|---:|---:|---:|---:|---:|
| 2.0 | 4 (6,8,10,12) | 2 (10,12) | 0 | **6** |
| **1.5** | 5 (5,6,8,10,12) | 3 (8,10,12) | 1 (12) | **9** |

**Total:**

| | bricks | combos/brick | total |
|---|---:|---:|---:|
| **Aug 2026 (log)** | 5 | 6 | **30** |
| **now (live)** | 8 | 9 | **72** |

**2.4× more backtests per symbol**, from two independent changes: the grid
widened 1.6× (5→8 bricks) and the pre-filter loosened 1.5× (6→9 combos/brick).

---

## 3. `min_rr` is 1.5 and is inert on the light path

Live value **1.5** (`config/feature_flags.yaml`, roadmap id=67 `min_rr_geometry_guard`:
*"RESOLVED 2026-10-10: min_rr=1.5 inert non-factor on light path"*). The range
1.5–1.9 was assessed ~2026-09-20 and deliberately left in code.

It is **not** inert on the heavy path: it is the grid pre-filter, so 1.5 vs 2.0
is the difference between 9 and 6 combos per brick — a 1.5× grid change. The
"inert" finding applies to light-path *qualification*, not to heavy-path
*search width*.

---

## 4. Why per-symbol time rose 12.7 s → 89.7 s

`nt_sweep_result.sweep_duration_ms` and the log's `DONE in Xms` are the **same
field** — `result.duration_ms` is set at `heavy_sweep.py:2073`, logged at
`:2075`, and persisted at `:2259`. So this is a like-for-like comparison:

| period | per-symbol median | source |
|---|---:|---|
| Aug 13–18 | **12.7 s** | orchestrator.log |
| Sept 16–30 | **242 s** | `nt_sweep_result` |
| Oct 1–16 | **89.7 s** | `nt_sweep_result` |

**Caveat:** `nt_sweep_result` contains **no heavy rows before 2026-09-16**, so
the August log is the only pre-September evidence and there is no Supabase
baseline for it.

**Attributed causes** (all confirmed live in the roadmap):

| cause | effect | evidence |
|---|---|---|
| ATR-anchored bricks (5→8) | ×1.6 grid | roadmap id=5, id=66 |
| `min_rr` 2.0 → 1.5 | ×1.5 grid | roadmap id=67 |
| **grid total** | **×2.4** | 30 → 72 combos |
| `dynamic_atr_mode` live | per-symbol solve | roadmap id=65 (2026-10-04) |
| `heavy_sweep_deadline_aware_scoring_mode` live | per-combo rescoring | roadmap id=19 (2026-09-12) |
| `dynamic_tier_realtime_optimization` | per-symbol work | roadmap id=95 (2026-10-10) |
| geometry harmonization `8097208` | per-combo cost | landed 2026-10-08 |
| PBM Phases A–E `93386b7` | per-symbol work | landed 2026-10-13 |

**Verdict: this is not a regression.** It is the accumulated feature work
landing — the grid is 2.4× wider and four live mechanisms add per-symbol
compute. The September figure (242 s) is the outlier; October improved to
89.7 s as the geometry work settled.

---

## 5. Config alignment (the actual code change)

`_DEFAULTS` in `regime_platform/core/feature_flags.py` had drifted from the
shipped YAML on six keys. The YAML always won at runtime — every accessor reads
`_load().get(key, _DEFAULTS[key])` — so **live behaviour was correct**. But the
defaults misrepresented reality, and a fresh environment with no YAML would
have silently run the **narrow** grid.

This is the same bug class as the unquoted-`off` incident (roadmap id=48), where
YAML 1.1 parsed the bareword `off` as boolean `False`.

| key | `_DEFAULTS` before | `_DEFAULTS` after | YAML |
|---|---|---|---|
| `min_rr` | `2.0` | **`1.5`** | 1.5 |
| `heavy_sweep_atr_anchored_bricks_mode` | `"off"` | **`"live"`** | live |
| `heavy_sweep_deadline_aware_scoring_mode` | `"off"` | **`"live"`** | live |
| `heavy_sweep_reachability_mode` | `"off"` | **`"shadow"`** | shadow |
| `atr_anchored_evidence_mode` | `"shadow"` | **`"live"`** | live |
| `dynamic_atr_consistency_tol` | `0.20` | **`0.2`** | 0.2 |

**No behaviour change** — the YAML already carried these values. The change
makes `_DEFAULTS` an honest fallback.

**Follow-up worth doing:** audit *every* `_DEFAULTS` key against the YAML, not
just these six. The comparison script used here is a two-liner (parse the YAML,
regex `_DEFAULTS`, diff). Any remaining mismatch is a latent narrow-grid class
of bug.

---

## 6. Consequence for capacity planning

**Do not size from a duration.** The heavy sweep is a **daily** job plus **intraday reopt
urgency** (§1), and its per-run cost is a function of the live flag set (§2) — so any
single number is a snapshot, not a spec.

What *is* stable:

| fact | why it holds |
|---|---|
| the grid is **72 combos** today | 8 bricks × 9 combos/brick under live flags |
| it grows with every ATR-anchor added | `generate_atr_anchored_brick_sizes` is additive |
| it is **CPU-bound and needs no GPU** | `renko/` has no torch/CUDA; TimesFM is the only GPU process |
| the light sweep is **~4 % duty cycle** | measured; invariant to concurrency |

The one number that scales with the roster is `symbols × combos`. At 50 symbols that is
~1,800 backtests — still trivial, but it is the figure to watch, not a duration.

**Sizing rule:** pick the box from the **steady-state RAM** (§5 of
`scale-and-concurrency.md`) and the **daily + intraday cadence**, then confirm the
per-run wall time in the *current* window before quoting it. Never carry a duration
forward from an earlier flag set.
