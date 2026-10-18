# Barrier model — where `barrier.py` fits into the orchestrator, and the five-phase build

**Date:** 2026-10-14
**Type:** Integration design + migration plan (design document; no code change at HEAD).
**Trigger:** operator request — *"repo is open. where does barrier.py fit into the orchestrator? write a detailed worklog + 5 phases."*
**Base:** implements `20261013_POSTERIOR_PRICING_METHODOLOGY_PROPOSAL.md` (Posterior Barrier Model, "one probability, one price, one gate").
**HEAD verified:** `4953e96` (13 commits past the review baseline `aa9fff1`; reopt harmonization + HARM-01..04 landed).
**Every line anchor below was re-read at this HEAD, not carried over from the review.**

---

## 0. Plain answer

**`barrier.py` does not exist at HEAD.** It is the *proposed* module (proposal §7: "one new module `barrier.py`, one service `posterior.py`, one table, one gate"). The name refers to the **closed-form double-barrier first-passage null**: driftless BM, σ from true-range ATR, π_W/π_L/π_E for *any* (tp_atr, sl_atr, T) in microseconds.

**It is not the repo's existing `first_passage.py`.** That file (`regime_platform/sweep_orchestrator/first_passage.py`, 269 LOC) is a different object — an *empirical* path-overlay that scores `tiered_atr.build_paths` arrays and shrinks toward 0.5 by transition carry. Its own header says it: *"LIVE WIRING: none. Phase 1 is offline-only — `walk()` needs a full 48-bar window, which a live signal does not have. Nothing in this module is imported by the sweep path."* It cannot be the live prior **by construction** (no-lookahead rule). `barrier.py` can: it needs only `(entry, tp, sl, ATR, T)` — all present at signal time. The name `barrier.py` (not `first_passage2.py`) is deliberate: the repo already spends "first passage" on the empirical object; the analytic object gets its own name.

**Where it fits: five attachment points, one per layer.** The orchestrator already has exactly one slot for each thing PBM replaces — geometry solve, pricing probability, EV substitution, outcome update, re-solve trigger. `barrier.py` + `posterior.py` slide into those slots; nothing else in the tick changes shape.

```
THE TICK (light)                          WHERE PBM ATTACHES
_process_symbol (orchestrator.py)
 ├─ :4186 dynamic_atr_pick()      ──①── objective inside select_tp_sl (geometry)
 ├─ :4232 lookup_bracket_probs()  ──②── posterior.read_cell() (pricing probability)
 ├─ :4334 compute_ev()            ── (demoted, unchanged — drift candidate only)
 ├─ :4494 apply_bracket_to_ev()   ──②── input swap (p_W,p_L,p_E) — ONE function, both paths
 ├─ :4556 apply_calibration_adj() ──④── retires in Phase D (conservative Kelly)
 ├─ :5358 p_win_bracket gate      ──③── EV lower bound > 0 (the one gate)
 ├─ :5804 _build_signal_sim_payload ── shadow-stamp point (Phase A)
 resolver (eod_resolver.py:943/:995) ──⑤── _update_cell_posterior — ALL outcomes incl. expired
 reopt (atr_reopt.py, monthly)       ──①── inherits the new objective automatically (37af9ee)
```

---

## 1. What changed since the review baseline (`aa9fff1` → `4953e96`) — and what didn't

**Landed (favourable to PBM):**
- **Reopt now calls the picker's own solve** (`37af9ee`): `atr_reopt._select` (:133), `solve_free` (:185), `band_health` (:217), `compare_to_current` (:255) all route through `tiered_atr.select_tp_sl` — "no second methodology". **Consequence for PBM: one objective change inside `select_tp_sl` automatically re-points the picker, the reopt, and the band-health invalidity diagnostic.** No second place to harmonize.
- **Era-cutover filter exists in reopt** (`atr_reopt.py:320 _fetch_cutover_ts`, `nt_calibration_cutover`): the era-leak fix pattern PBM needs at the posterior read — copy it, don't invent it.
- **Auto-apply clamps restored** (`ffdfffa`): `atr_reopt.py:702` `auto_apply_clamps` default True; monthly 1st 03:30 UTC (`atr_reopt.py:16`).
- **First harmonized reopt report** (`worklog/kairos/reports/20261013_atr_reopt.md`): **three shipped bands are INVALID — band EV ≤ 0 on the live intrabar basis: XAUEUR −0.235, XAUUSD −0.582, US30USD −1.592.** The shipped bands are now *measurably* dead on the platform's own solve — Phase C's continuous argmax is not replacing healthy machinery.
- HARM-01..04 (asset-class vocabulary, deadline floors, label-refresh cadence): router-side, orthogonal to PBM.

**Not landed (the P0s from the triage — still open at HEAD, re-verified line by line):**

| P0 | Anchor at HEAD | Status |
|---|---|---|
| Heavy-path EV clobber | `heavy_sweep.py:1506-1536` — `apply_calibration_adjustment` overwrites `ev_result.p_win`/EV with **no** `_bracket_applied_to_ev` guard; the recompute at `:1528` is called **without `p_expire`** (drops the expiry term entirely) | **open** — light path has the guard (`orchestrator.py:4581-4590`, bracket-EV-terminal), heavy does not |
| Expired excluded from calibration | `eod_resolver.py:943-944` and `:995-996` — `_write_calibration_record` only on `tp_hit`/`sl_hit`; docstring at `:722-727` states the exclusion as policy | **open** — survivorship by construction |
| Pooled-side lookup | `p_win_bracket.py:203` — `signal=eq.all` on `nt_bracket_reachability`; migration `159_reachability_signal_side_and_outcome_by_side.sql` columns unused by the live path | **open** |
| Scored-dead publishes | `feature_flags.yaml:905` `p_win_bracket_mode: "shadow"` (gate stamps `p_win_bracket_ok`, doesn't veto) while `p_win_in_ev_mode: "live"` (:931) already prices on the bracket | **open** — the gate/pricing asymmetry |
| Gate stack hardened | `dynamic_atr_consistency_mode: "live"` (:518), `dynamic_atr_walkforward_mode: "live"` (:530) | both live — *more* gates to retire in Phase C |

---

## 2. The call chain at HEAD, with anchors (what PBM replaces, slot by slot)

### 2.1 Light path — every tick, `orchestrator.py` `_process_symbol`

| step | anchor | what happens | PBM slot |
|---|---|---|---|
| geometry | `orchestrator.py:4186` → `tiered_atr.py:812 dynamic_atr_pick` | the ONE solve (G3): store read `:869` (`read_pick_from_store`, 6h TTL, `_pick_in_band` G1 boundary `:778`) → `min_signals: 60` floor `:901` → **band-centre cliff** `_band_center_pick :758` → envelopes `:953` (intrabar, sl_first) → **grid argmax** `select_tp_sl :464-558` (9-12 points, both-halves, `max_expiry 0.20`, `cost_atr 0.1`) → G2 fallback `:1002` → **consistency gate** `:1007-1047` (live) → **walk-forward gate** `:1055-1095` (live) → `write_pick_to_store :1100` | **① objective** — Phase C |
| applied | `orchestrator.py:4197-4199` | `trade_signal["take_profit"/"stop_loss"]` overwritten in live mode | unchanged |
| probability | `orchestrator.py:4232` → `p_win_bracket.py:169 lookup_bracket_probs` | `nt_bracket_reachability` row `(symbol, vol_tier, bucket, signal=all)` → raw `pct_hit_tp` as `p_win_bracket` (`:148-149`, all_in source) | **② read** — Phase A shadow / Phase B swap |
| directional EV | `orchestrator.py:4334` `compute_ev()` | 3-source logit blend (p_regime/p_timesfm; markov off) | demoted — unchanged |
| EV substitution | `orchestrator.py:4494` → `p_win_bracket.py:291 apply_bracket_to_ev` | mutates `ev_result` in place with the **3-outcome recompute** (`p_expire` from `p_resolve` via `expire_prob :284`) | **② swap point** — input changes, contract stays |
| enforcement | `orchestrator.py:4556-4616` | `apply_calibration_adjustment`, **guarded** `:4581` (bracket EV terminal) | retires Phase D |
| gate | `orchestrator.py:5358-5410` | `bracket_gate_decision` (`p_win_bracket.py:244-262`), mode `shadow` → stamps only | **③ the one gate** — Phase B/C |
| stamp | `orchestrator.py:5804` → `_build_signal_sim_payload :978` → `:1147-1159` | `p_win_dir / p_win_bracket / p_resolve / p_win_resolve / bracket_bucket / bracket_vol_tier` → `nt_signal_sim` | **shadow-stamp point** — Phase A |

### 2.2 Heavy path — `heavy_sweep.py` (twin, and the clobber)

Same store, same probability source, `apply_bracket_to_ev` at `:1474-1491` (correctly invoked), then `apply_calibration_adjustment` at `:1506-1536` — **unguarded**: `ev_result.p_win = calib_adj["p_win"]` (`:1526`) reprices a bracket-priced EV from the directional number, and `recompute_ev_fields` at `:1528` is called **without `p_expire`** — the heavy EV silently becomes a 2-outcome price. This is the exact light/heavy divergence the triage flagged; PBM closes it **structurally** (one EV definition, one substitution function, both paths).

### 2.3 Update loop — the measurement half

- Nightly EOD-lookback (`:17`, job `eod-lookback`, `orchestrator.py:1498`) + hourly calibration-resolve (`:38`, `orchestrator.py:1792`) → `eod_resolver.resolve_pending_signals` → `_resolve_one :940-944` / `_resolve_one_bars :992-996`: `_patch_signal` (outcome → `nt_signal_sim`) then `_write_calibration_record :717` **only for tp_hit/sl_hit**.
- Weekly table build: Sun 07:00 UTC `bracket-reachability-weekly` (`orchestrator.py:1663`) → `_run_bracket_reachability :2554` → `reachability_prob.build_reachability_table :297` (delete-then-insert `nt_bracket_reachability`).
- **⑤ posterior hook:** one new call beside `_write_calibration_record`, fired for **all three outcomes**. One hook covers both schedulers (they share the `_resolve_one*` funnel). The weekly build gains a decay-rebuild twin (or retires in Phase C).

### 2.4 Reopt — monthly, auto-apply

`atr_reopt.py:457 run_atr_reopt` (monthly 1st 03:30, `:16`) → `solve_free :185` (free clamps through `select_tp_sl`) + `band_health :217` (shipped band scored by the same solve) → proposals → `apply_symbol_clamps :568` patches `feature_flags.yaml` when `auto_apply_clamps` (`:702`, default True) and drift > 15%. Evidence basis era-filtered via `_fetch_cutover_ts :320`. **① inherits the new objective automatically; trigger becomes evidence-based in Phase C.**

---

## 3. The five attachment points (the design core)

### ① Geometry — inside `select_tp_sl` (`tiered_atr.py:464`)
The double loop `for tp in tp_grid: for sl in sl_grid: walk_envelopes(...)` (lines `507-538`) becomes a **bounded continuous argmax of posterior EV** over `[tp_atr_min, tp_atr_max] × [sl_atr_min, sl_atr_max]`, σ from true-range ATR. The band clamps and `min_rr` survive — as the *search domain*, exactly as the proposal says ("the operator bands survive as the search domain, not as a grid of testable points"). Store read/write and the 6h cache survive — they cache *solves*, not methodology. Because reopt routes through this same function (`37af9ee`), picker + reopt + `band_health` all re-point with one change. The consistency and walk-forward gates (`:1007-1095`) demote to diagnostics: the walk-vs-barrier replay agreement becomes a **standing acceptance test**, not a selection branch.

### ② Pricing probability — beside/under `lookup_bracket_probs` and `apply_bracket_to_ev`
Phase A: `posterior.read_cell(symbol, side, vol_tier, bucket, deadline_class, era)` → `(p_W, p_L, p_E, n_eff, ci)` from the new `nt_cell_posterior` table, shadow-stamped at the `:1150` payload block. Phase B: `apply_bracket_to_ev`'s inputs change from `(p_win_bracket, p_resolve)` to the posterior triple + `m_mtm_expire`. **The mutation contract stays identical** — which is why this is a small diff at exactly two call sites (`orchestrator.py:4494`, `heavy_sweep.py:1478`). Fail-open semantics become fail-*honest*: an unscored cell prices at the analytic null (usually −EV after costs → no-trade), not at `None` → directional fallback.

### ③ Qualification — the gate block (`orchestrator.py:5358-5410`, heavy `_evaluate_heavy_qualification :1544`)
`bracket_gate_decision`'s three-way `pass / reject_low / reject_unscored` and the `min_p_win_bracket` floors are replaced by **one inequality: the 5% lower credible bound of posterior EV > 0**. Operational gates (cooldown, dedup, regime non-neutral) stay. The unscored/scored asymmetry — today's `fail_closed` cliff vs `shadow` non-veto contradiction — dissolves because every cell *has* a posterior.

### ④ Update — `eod_resolver.py:943/:995`
`_update_cell_posterior(sig, resolved)`: Dirichlet increment on `(symbol, side, vol_tier, bucket, deadline_class, era)`, fired on `tp_hit`, `sl_hit`, **and `expired`** (MTM at expiry — the resolver already computes it). This is the survivorship fix *and* the single-key fix in one hook: migration 159's side columns finally get a consumer, `signal=eq.all` pooling retires.

### ⑤ Reopt trigger — `atr_reopt.py:673/:702`
Calendar monthly → **evidence-triggered**: re-solve when ≥ k new resolved outcomes accumulate per symbol, or posterior KL vs the solved-at posterior exceeds threshold. `band_health` stays as the invalidity diagnostic (it just proved three bands dead). Auto-apply clamps survive but act on a *search domain*, not a grid.

### Where `barrier.py` deliberately does NOT attach
- **Not into `first_passage.py` / `fp_eval.py`** — the empirical overlay is an offline Phase-2 candidate; `barrier.py` must not import it, and vice versa. Two objects, two names, two jobs.
- **Not into `compute_ev`'s blend** (`services/statistics/ev_engine.py`) — the directional blend is demoted to drift candidate; it may only enter later as μ (Phase E), gated on measured barrier-orientation AUC.
- **Not as a new gate** — it *retires* gates; adding it to the stack would be the anti-pattern the triage documented.

---

## 4. The module contract (`barrier.py`, ~150 LOC, pure functions, no I/O)

```python
@dataclass(frozen=True)
class BarrierNull:
    p_win: float          # π_W = P(touch +u before −d, before T)
    p_loss: float         # π_L
    p_expire: float       # π_E = 1 − π_W − π_L
    u: float              # log(TP/entry)   > 0
    d_abs: float          # log(entry/SL)   > 0
    horizon: float        # T·σ²  (the only normalization that matters)

def barrier_null(tp_atr, sl_atr, *, atr, T: int = EXPIRY_BARS, sigma: float | None = None) -> BarrierNull
def ev_from_null(null, tp_atr, sl_atr, *, cost_atr) -> float        # cold-book price
def ev_lower_bound(posterior, tp_atr, sl_atr, *, cost_atr, q=0.05) -> float  # the gate
```

- **σ = true-range ATR / price.** D6 fix: `compute_atr` is close-only `mean|Δclose|` today; OHLC is already fetched by `build_envelopes` — reuse it.
- **μ = 0.** Frozen default; the measured record (AUC 0.499) earns nothing else.
- Two-sided first passage by the method-of-images alternating series; converges in a handful of terms at these horizon-normalized distances (`4.5 ATR / √48 ≈ 0.65`).
- **Sanity anchors as unit tests** (proposal §2): gambler's-ruin limit `π_W → d_abs/(u+d_abs)` as T→∞; symmetric barriers → 0.5; scale invariance in `u/(σ√T)`; **walk-agreement replay** — `walk_envelopes` vs `barrier_null` on the same historical windows, mean |Δπ_W| bounded (the standing test that makes the envelope walk the *simulator*, not the selector).
- Placement: `regime_platform/sweep_orchestrator/barrier.py`, beside `tiered_atr.py` (it imports only `EXPIRY_BARS` and numpy/math).

`posterior.py` (~200 LOC): `read_cell`, `update_cell` (decay-weighted Dirichlet, `k0=30`, `τ≈14d`), hierarchical shrinkage of the selection multiplier `f_c = p_W/π_W` up symbol → class → 1, and the table DDL for migration **210** `nt_cell_posterior` (schema in proposal §3).

---

## 5. The five phases — concrete diffs, acceptance bars, rollback

Each phase is one lever, replay-verified, reversible — the repo's own G1-G4 discipline applied to the redesign. Order matters: **measurement quality first, price second, geometry third, sizing fourth, drift last.**

### Phase A — build, shadow-stamp, and the P0 fixes (measurement quality)

**Changes**
1. New `barrier.py` + `posterior.py` + migration `210_nt_cell_posterior.sql`.
2. Shadow-stamp in `_build_signal_sim_payload` (`orchestrator.py:1147-1159` block): `pbm_p_w / pbm_p_l / pbm_p_e / pbm_null_w / pbm_gap / pbm_n_eff`. No live effect — same pattern Batch C used for `p_win_dir`.
3. Resolver hook `_update_cell_posterior` at `eod_resolver.py:943` and `:995` — **all outcomes, expired included**.
4. True-range ATR (`tiered_atr.compute_atr` → TR basis on the OHLC already fetched) + per-symbol spread cost `c` replacing flat `cost_atr: 0.1`.
5. **P0 fixes in the same phase** (proposal §10: "a better methodology priced through a corrupted chain is still corrupted"):
   - replicate the `:4581` guard at `heavy_sweep.py:1525` and pass `p_expire` at `:1528`;
   - era filter on the posterior read, mirroring `atr_reopt.py:320`;
   - EV-floor parity light/heavy.

**Acceptance bar (era-clean replay):** null sanity tests green; walk-vs-barrier agreement bounded; posterior reliability ≥ table lookup on the resolved book; **zero publish-behaviour change** (all stamps shadow).
**Rollback:** `pbm_mode: off` — stamps stop; nothing else moved.

### Phase B — swap the EV input (one flag, one function, both paths)

**Changes:** `p_win_in_ev_mode` gains `posterior` (or sibling `pbm_ev_mode`): `apply_bracket_to_ev` inputs become the posterior triple at `orchestrator.py:4494` + `heavy_sweep.py:1478` — **one function, both paths**; old lookup kept as bake-period fallback. With one EV definition the heavy-clobber class of bug becomes structurally impossible.

**Acceptance bar:** replay EV monotonicity (no unexplained sign flips on the resolved book); heavy/light EV parity test in CI; publish-volume change explained cell by cell.
**Rollback:** flag back to `live` (table source).

### Phase C — continuous geometry, gate retirement, evidence-triggered reopt

**Changes:** `select_tp_sl` objective → posterior-EV argmax over the band domain (attachment ①); grid, band-centre fallback, consistency and walk-forward gates demote to diagnostics (flags to `diagnostic`); `p_win_bracket` gate block (`:5358-5410`) replaced by the EV lower bound; reopt trigger evidence-based (attachment ⑤); weekly reachability build → posterior decay rebuild.

**Acceptance bar:** replay book EV ≥ grid-book EV; **band-centre rate 68/77 → 0**; every active symbol ships an in-band solved geometry; the three INVALID bands (XAUEUR −0.235, XAUUSD −0.582, US30USD −1.592) either solve in-band or price no-trade — honestly.
**Rollback:** objective flag back to grid; gates back to `live`.

### Phase D — retire enforcement → conservative Kelly

**Changes:** sizing consumes the EV lower bound (thin evidence → wide interval → small size); delete `calibration_enforcement.py` (656 LOC) and both (by then single) call sites; `calibration_brier` finally gets its reader (reliability dashboard).

**Acceptance bar:** sizing sanity on replay (thin cells small, zero-evidence ≈ 0); no publish-volume explosion.
**Rollback:** `pbm_sizing_mode` back to enforcement (module kept in history).

### Phase E — drift layer, measured or nothing

**Changes:** `p_dir` admitted only as μ on the barrier model, only on walk-forward **barrier-orientation AUC above the null**; LLM suppressor (`p_clef_wait`, the one measured-positive output) judged against the same bar.

**Acceptance bar:** promote only above null. **Expected honest outcome: stays off** (AUC 0.499, `p_clef_dir` 0.58 = chance — the rule generalizes).
**Rollback:** μ pinned to 0 — the stable resting state.

---

## 6. Deletion ledger (net code out, not in — the "Claude diet")

| Retiring | LOC | Anchor at HEAD | Replaced by |
|---|---|---|---|
| `select_tp_sl` grid + `_band_center_pick` + consistency/walk-forward *gates* | ~400 | `tiered_atr.py:464-558, :758, :1007-1095` | `barrier.py` (~150) + continuous argmax |
| `p_win_bracket` lookup + gate decisions | ~323 | `p_win_bracket.py` | `posterior.py` (~200) |
| `calibration_enforcement.py` | 656 | (heavy call `heavy_sweep.py:1506`) | conservative Kelly |
| `calibration_bias_bucket.py` resolved-only bias | 460 | survivorship anchor `eod_resolver.py:717-727` | posterior update + reliability QA |
| Store-read/TTL/stale-fallback machinery | ~200 | `tiered_atr.py:710-711, :869` | event-driven `nt_cell_posterior` |

**Net ≈ 1,700 LOC out, ~350 in.** One module, one service, one table, one gate.

---

## 7. Honest boundaries (carried from proposal §10, re-stated because they gate the plan)

- **PBM does not manufacture edge.** μ=0 + positive costs → most cells price below the bound → sparse/no-trade book. That is the *correct* output for the current entry layer: measured TP rates sit below the random-walk null (13-15% vs ~37.5% first-touch at the shipped bands). The selection-gap KPI (`pbm_gap`) is the entry-timing thermometer (R1, strategy layer).
- **Phase A must carry the P0s.** Heavy clobber + survivorship + era leak, or the methodology is priced through a corrupted chain.
- **The null is a model** (no jumps, no gaps, constant σ within the deadline). The empirical layer exists to absorb systematic deviation; the intrabar resolver keeps pricing gap-through-SL risk.
- **The drift layer will likely stay null** — by design, that is a stable resting state, not a failure.

---

## 8. Files / lineage

- Proposal: `20261013_POSTERIOR_PRICING_METHODOLOGY_PROPOSAL.md` (kept with the review set; not in this repo's `worklog/`).
- Grounding triage: `20261013_KAIROS_TRIAGE_CALIBRATION_GEOMETRY_REACHABILITY.md`; mechanism review: `20261013_GEOMETRY_MECHANISM_FORMULATION_REVIEW_AND_LLM_SUPPLEMENT.md`.
- Repo's own corroboration at HEAD: `worklog/20261013_GEOMETRY_REVIEW_VERIFICATION_AND_ERA_CLEAN_REPLAY.md` (D1/D2/D4/D5/D6 verified; D3 corrected — expiry never uniquely load-bearing), `worklog/kairos/reports/20261013_atr_reopt.md` (3 INVALID bands), `worklog/20261013_HARMONIZATION_SINGLE_SOURCE_OF_TRUTH.md` (HARM-01..04).
- Code anchors at HEAD `4953e96` (all personally re-read this session): `orchestrator.py:4186/4232/4334/4494/4556-4616/5358-5410/5804`, `tiered_atr.py:464-558/758/812-1103`, `p_win_bracket.py:169-206/244-262/291-323`, `heavy_sweep.py:1474-1536`, `eod_resolver.py:717-727/940-944/992-996`, `atr_reopt.py:133/185/217/320/457/568/702`, `feature_flags.yaml:518/530/557-559/646/905/931`, migrations `159`, `209` (last).
