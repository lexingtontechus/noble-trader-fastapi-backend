# TimesFM rebuild T4 — the direction decision (build log)

**Date:** 2026-10-28
**Type:** Build (T4 of the rebuild plan — "the direction decision, separate
scope"; design = `worklog/20261028_KAIROS_TIMESFM_REBUILD_DESIGN.md` §5 T4;
ladder semantics = `docs/scopes/20261023_TSFM_VS_REASONING_LLM_AND_ROUTER_EDGE.md`).
**Scope:** `timesfm_blend_mode: "live" → "shadow"` — p_timesfm is **withheld
from the directional EV blend on both cadences** (the unmeasured ~50 % blend
weight cut, renormalized exactly), while the fetch + the router-edge skill
scoring **continue** (the A/B measurement keeps accumulating so the
sanctioned live-stream number still arrives). One yaml line + one
config-state test assertion; zero code changes — the ladder was fully built
in R2 (2026-10-23) and heavy/light apply it byte-exactly.

---

## 1. What landed (2 files)

| File | Change |
|---|---|
| `config/feature_flags.yaml` | `timesfm_blend_mode: "shadow"` (from `"live"`). Comment block rewritten as the T4 flip record: operator decision; the evidence basis (below) recorded; the sanctioned live-stream number marked PENDING production stamps; the R2 scope's next-rung gates stated; code fallback stays `"live"`. |
| `tests/test_router_edge.py` | `TestFlags::test_yaml_values` — the shipped-config assertion `timesfm_blend_mode == "live"` → `"shadow"`, comment records the flip and points at the yaml/worklog evidence trail. `_DEFAULTS` assertions unchanged (`"live"` — the code fallback). |

**Zero changes** to: `ev_engine.py` (the renormalizing logit-pool — unchanged
since R2), `orchestrator.py` (the gating expression at :4963 predates the
flip and reads the flag at call time), `heavy_sweep.py` (:702/:1584 same),
`router_edge.py` (the skill accumulator is blend-mode-independent),
`timesfm_map_mode` (still `zmap_live` — the z-map path is NOT part of T4),
the z-map code, the claims layer, the resolver, migrations.

## 2. The skill audit — run first, recorded honestly

`python -m scripts.measure_router_edge` executed in this sandbox before the
flip:

```
(no cells yet — accumulate stamps first)
aggregate n_eff: router=0  timesfm=0
VERDICT: insufficient evidence (n_eff < 30) — keep accumulating stamps.
```

The machinery is executable and honest; the sanctioned live-stream number
(`timesfm_skill` = acc − majority on the stamped outcome stream, W-X2
convention) lives in the production box's `config/param_store.sqlite`, which
is box-local and never committed. **The number does not exist in this
sandbox — recorded, not assumed.**

The evidence that DOES exist, and the basis of the operator's decision:

| Evidence | Number | Instrument |
|---|---|---|
| Lab direction lane (the TSFM Probabilistic Forecast Lab, 90 rolling origins, DM-tested) | Brier **0.2686** vs coin 0.2497 — measured no-skill, worse than a coin | the design doc §0.2's table |
| Post-deploy live probes (2026-09-17, `worklog/20260917_TIMESFM_PROBE_AND_CADENCE_CLARITY.md`) | `p_timesfm` present on **100 %** of sweeps, range **0.496–0.501** — a z-map-saturated near-coin signal | production log probe |
| The z-map's own history (`timesfm_map_mode` yaml:61-65, `worklog/20260917_TIMESFM_ZMAP_FIX.md`) | the raw sigmoid mapping saturated at p≈0.5 — the zmap exists BECAUSE the raw output carried no directional signal | config archaeology |

The design's T4 rule ("Skill ≤ 0 ⇒ flip `timesfm_blend_mode: "shadow"` …
either way the number exists first") is applied on the operator's explicit
instruction, with this basis and this caveat recorded: the lab Brier and the
probe range are NOT the same instrument as the live-stream acc−majority
number. Shadow is deliberately the **A/B rung, not the exit** — the fetch and
the `_timesfm` skill accumulator keep running, so the sanctioned number
arrives while the unmeasured weight is already cut. The R2 scope's own gates
then arbitrate the next rung: `off` at `timesfm_skill < 0` and n_eff ≳ 100
(kills the :8002 dependency too), back to `live` if the number surprises.

## 3. Verification (offline; 22/22)

Throwaway suite `/home/z/verify/t4_verify.py` (real yaml for the blend mode;
the loader is patched only for the fallback-vocabulary check; param_store
redirected to a tempdir for the tick test — the repo tree stays clean):

- **K (7)** — the flip surfaces: yaml carries `shadow`;
  `get_timesfm_blend_mode()` reads it (mtime hot-reload, no restart);
  `_DEFAULTS` stays `"live"`; garbage → `"live"` (the vocabulary guard);
  `timesfm_map_mode` untouched (`zmap_live`); `router_edge_mode` untouched
  (`shadow`); loader restore re-checked.
- **G (4)** — the gating expressions verbatim from the code under the real
  yaml: light `_tsfm_blend_for_ev = p_timesfm if mode == "live" else None`
  → None (orchestrator.py:4963); light fetch gate `mode != "off"` → fetch
  continues; heavy `p_timesfm if _heavy_tsfm_mode == "live" else None` → None
  (heavy_sweep.py:1584); heavy fetch gate → fetch continues (result.p_timesfm
  observability preserved).
- **E (7)** — EV byte-exactness against manual pools: live blend == the
  manual 3-source logit-pool (5/14 regime + 4/14 markov + 5/14 timesfm)
  stamped at the house 4dp; shadow blend == the manual **renormalized**
  2-source pool (5/9 + 4/9 — the cut, renormalized exactly); `sources_used`
  membership flips correctly; live ≠ shadow (the cut is real); the engine's
  default weights untouched; the short-inversion convention intact on the
  live side; `p_router` never enters (no caller passes it).
- **S (4)** — skill-accumulator continuity under the full T4 state
  (`router_edge_mode=shadow` + `timesfm_blend_mode=shadow`, both from the
  real yaml): a stamp tick + a resolve tick with `p_timesfm` fetched in
  shadow → the `_timesfm` SkillCell **gained a scored count** and `_last`
  carries the stamped `p_timesfm`. The A/B measurement keeps accumulating —
  the sanctioned number still arrives.

The 3 initial failures were TEST tolerance bugs (my 1e-15 ulp vs the
engine's `round(p_win, 4)` house stamp at ev_engine.py:795). **Zero
implementation bugs.**

## 4. Regressions

- T4-relevant: `test_router_edge.py` (full R2 suite, including the updated
  config-state test) + `test_timesfm_zmap.py` + `test_heavy_sweep_shadow_
  mode_composition.py` + `test_w_c1_shipped_ev.py` + `test_ev_engine_
  recompute.py` → **69 passed / 0 failed**. `test_leverage_eval.py` 50/50.
- Claims-adjacent set (`eod_resolver`, `settlement_contract`,
  `eod_lookback_wiring`, `eod_resolver_basis`, `recalib`,
  `eod_timesfm_config_migration`, `vol_backtest`): 132 passed / 11 failed —
  stash round-trip proves the failure set **byte-identical at HEAD**
  (pre-existing, `arch` absent in this sandbox; same set as T1/T2/T3).
- ruff on `tests/test_router_edge.py`: 2 errors, both **pre-existing at HEAD**
  (stash-verified — I001 import order + F401 unused import, untouched by this
  change). `py_compile` clean; yaml parses.

## 5. Operator actions

- **None required for the flip itself** — mtime hot-reload, no restart; both
  cadences read the flag at call time.
- The A/B window (R2 scope §4): let shadow run across a full refit window;
  compare `p_win_raw`/EV deltas vs the live-era rows; run
  `python -m scripts.measure_router_edge` (or `--json`) on the production box
  where the param_store stamps live — the verdict banner states which gate
  the numbers support. `scripts/pulse_check_r2.py` prints the same ladder.
- Next rungs, gated exactly as the R2 scope states: `off` (exit — kills the
  :8002 fetch entirely) only on `timesfm_skill < 0` at meaningful n_eff;
  back to `live` if the sanctioned number surprises. Nothing auto-flips.

## 6. Honest limitations

- The flip precedes the sanctioned live-stream number — an operator call
  (this instruction), recorded with its basis above rather than silently
  satisfied. The protections: shadow is reversible with one line; the
  accumulator keeps running so the number arrives regardless; the yaml
  comment + this worklog + the git diff carry the full audit trail.
- Shadow keeps the network cost (fetch per sweep, cached 15 min) and the
  :8002 dependency — deliberate, because killing them (`off`) is the exit
  rung that wants the number first.
- Published `p_win_raw`/EV values change by construction from the live-era
  rows (the renormalized blend is a different function of the remaining
  sources — E4 proves the change is exactly the renormalization, nothing
  else). EOD-correction bias bands read `predicted_p_win_raw`; a regime
  change in that column is expected and watchable, not a bug.
- The probes/Brier evidence is not the acc−majority instrument; if the
  sanctioned number eventually disagrees, the yaml flip-back is one line and
  the ledger/history keeps both eras auditable.
- Heavy/light `p_win_raw` divergence note (R2 scope §6) is unchanged: heavy
  has no `p_router` source; both now withhold `p_timesfm` identically.
