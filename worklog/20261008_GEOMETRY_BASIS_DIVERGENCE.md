# Worklog: Geometry Basis Divergence — the Picker Measures a Different Event Than the Resolver

**Date:** 2026-10-08
**Scope:** Root cause of the negative-expectancy geometry picks (GBPUSD/USDCAD sell case); harmonization plan
**Related:** `worklog/20261008_PULSE_CHECK_AND_PIPELINE_EVALUATION.md` (§6.1 C2/C3), `worklog/20261002_SIGNAL_QUALITY_PLAN.md`, `worklog/20261008_PARAM_SOURCE_OF_TRUTH.md` (same "one source of truth" shape)

---

## 0. The user's three corrections, taken as the frame

1. **This encompasses C2** — yes. C2 ("widen bracket geometry or tighten TP") was premature: you cannot evaluate a bracket change while the chooser measures on the wrong basis. C2 and C3 are one issue.
2. **intrabar vs dynamic_atr calcs need harmonizing** — yes, and it is exactly the single-source-of-truth shape already applied to the param store: one implementation, imported by every consumer. Today there are **four** implementations with **three** different semantics.
3. **geometry ≠ reachability** — correct, and my earlier write-up conflated them. They are two different mechanisms and only one is broken:
   - **Reachability** (`bracket_reachability.py`) reads **stored `outcome` from `nt_signal_sim`** — i.e. it measures the *live resolver's* verdict. Its basis is **correct**.
   - **Geometry** (the TP/SL *choice* — `tiered_atr.py` / `heavy_sweep.py`) **re-derives outcomes from prices with its own rule**. Its basis is **wrong**.

   Reachability already contains the truth (GBPUSD sell, 4+ bucket: TP 5%, expired 86%). The optimizer is not reading its own platform's measured truth — it re-derives a different one.

---

## 1. The question: "the design is realtime — how is it getting this so wrong?"

It is **not** the realtime part. The optimizer is realtime and internally consistent. The defect is that **its internal model measures a different event than the one that settles**. Realtime then makes it confidently wrong *faster* — it re-picks every 4h on a signal it scores +0.61 ATR/signal that actually realises −0.24.

Four sites resolve a bracket, and they do not agree:

| # | Site | Basis | Same-bar tie |
|---|---|---|---|
| 1 | `eod_resolver.resolve_signal_outcome_ohlc` (LIVE, `eod_resolver_basis: intrabar`) | **high/low** | **SL-first** (hardcoded) |
| 2 | `eod_resolver.resolve_signal_outcome` → `settlement_contract.settle_from_closes` | closes | `tp_first` (honours `settlement_same_bar_policy`) |
| 3 | `tiered_atr.walk` — **the geometry picker** | **closes only** (`build_paths` uses `b["c"]`) | **TP-first** (`ti <= si`) |
| 4 | heavy-sweep grid → `renko/risk_manager` | brick **close** only | **SL checked before TP** |

Sites 1 and 3 are the pair that matter: the picker (3) chooses, the resolver (1) settles. They differ on **both** axes — basis *and* tie-break.

---

## 2. Measured: the gap is entirely measurement error

Replayed the **same 234 GBPUSD sweep-light signals**, same grid, same clamps, varying only basis and tie-break (`tiered_atr.walk` semantics, 48-bar expiry):

| TP / SL | close / TP-first **(picker)** | close / SL-first | intra / TP-first | intra / SL-first **(live)** |
|---|---|---|---|---|
| 1.65 / 0.35 | WR 47.9% · **+0.607** | 47.9% · +0.607 | 26.5% · +0.180 | WR 5.6% · **−0.239** |
| 1.71 / 0.35 | 47.0% · +0.618 | 47.0% · +0.618 | 25.6% · +0.178 | 5.6% · −0.236 |
| 1.75 / 0.35 | 46.6% · +0.628 | 46.6% · +0.628 | 24.8% · +0.171 | 5.6% · −0.233 |
| 1.80 / 0.40 | 46.6% · +0.625 | 46.6% · +0.625 | 25.2% · +0.155 | 7.7% · −0.231 |

At the live bracket (TP 1.61 / SL 0.34):
- **picker sees** ≈ WR 48%, **+0.61 ATR/signal**
- **live realises** ≈ WR 6%, **−0.24 ATR/signal**
- live 7d actual for GBPUSD sell: **WR 6.4%**, MAE/SL 2.3× — matches `intra/SL-first` almost exactly.

**The entire 0.85 ATR/signal gap is measurement, not market.** Two independent distortions stack:

1. **Basis (closes → high/low):** 47.9% → 26.5% WR. A 0.34-ATR stop is inside a 1h bar's own range (~1 ATR), so the wick that trips it is invisible to a close-walk.
2. **Tie-break (TP-first → SL-first):** 26.5% → 5.6% WR. A **single 1h bar first-touches both levels in 21.4% of episodes** at this bracket — and on those the tie-break alone decides the outcome.

Note the picker's own grid is *insensitive* to the tie-break (close/TP-first ≡ close/SL-first): a close-walk can't have a same-bar tie because it only ever tests one price per bar. That is precisely why the defect is invisible from inside the picker.

---

## 3. Why the tight stop looks good and is fatal

The FX band (`dynamic_atr.by_class.forex` / `by_symbol.FOREX`) clamps `sl_atr ∈ [0.30, 0.45]`, `tp_atr ∈ [1.6, 1.85]`. On a close-walk that shape is a high-WR, high-RR machine (+0.6 ATR). On the intrabar basis with SL-first, the same shape is a **stop-collection device**: the stop sits inside the noise, so the 21.4% same-bar case resolves against it and the routine wick does the rest.

So the config isn't "wrong" — it is **correct for the basis it was tuned on and catastrophic on the basis that runs.** The optimizer will never find a better SL while it optimizes against wicks it cannot see.

---

## 4. Fix plan — one resolver, imported everywhere

Same shape as the param-store work: a single implementation, one call site per consumer, no parallel re-derivations.

**Stage 1 — single resolver primitive.**
Extract one `resolve_bracket(entry, sl, tp, direction, bars, *, basis, same_bar_policy, expiry_bars)` into `settlement_contract.py` (it already owns the OCO semantics and the policy flag). Every other site imports it:
- `eod_resolver` (live + close-walk + shadow) — delete the duplicate `resolve_signal_outcome_ohlc` walk
- `tiered_atr.walk` — call it with `basis="intrabar"`, `same_bar_policy=<flag>`; `build_paths` must carry `h`/`l`, not just `c`
- heavy-sweep grid (`renko/risk_manager`) — align to the same first-touch rule (its brick-close walk is a third basis)

**Stage 2 — one basis flag, read everywhere.**
`eod_resolver_basis` + `settlement_same_bar_policy` become the only two knobs. Delete the hardcoded SL-first in `resolve_signal_outcome_ohlc` (it silently ignores the policy flag the close-walk path honours). If SL-first is intended for intrabar, set the flag to `sl_first` for all sites — do not hardcode it in one.

**Stage 3 — persist the ambiguity.**
`resolve_signal_outcome_ohlc` already computes `ambiguous` (bar spans both levels) and then **discards it** — no `ambiguous` column on `nt_signal_sim`. Persist it. The 21.4% same-bar population is the single most decision-relevant fact about this geometry and it is currently invisible.

**Stage 4 — re-derive geometry on the harmonized basis.**
Only after 1–3: re-run the FX `sl_atr` band against the intrabar+policy basis. Expect the band to move materially wider (a 0.35-ATR stop cannot survive a ~2 ATR median adverse excursion). Re-run the geometry replay (roadmap id 71) on the harmonized basis first — its earlier "TP 9–17 / SL 0.5–2 positive both halves" finding is a **close-walk** result and must be re-measured.

**Stage 5 — the picker should prefer measured outcomes over re-derivation.**
`nt_bracket_reachability` already holds live-basis measured TP/SL/expiry rates per `(symbol, vol_tier, distance_atr_bucket)`. The picker's candidate walk is still needed (it scores candidate brackets, reachability buckets realised ones), but the two must be *consistent* — a candidate whose realised bucket shows TP 5% / expired 86% must not score +0.6 on the walk. Add a cross-check gate: a pick is rejected if the walk's implied expectancy contradicts the reachability bucket it lands in beyond a tolerance. That is the "does the optimizer agree with its own platform's measurements" test.

---

## 5. What NOT to do

- **Do not disable GBPUSD/USDCAD.** The failure is basis-wide, not symbol-specific (every FX symbol carries the same clamped band). Disabling two symbols hides it.
- **Do not "widen the SL" as a standalone lever (C2 as written).** You would be guessing against a broken measurement. Harmonize first, then re-derive.
- **Do not treat this as a reachability problem.** Reachability is measured correctly. It is the *geometry chooser* that is wrong.
- **Do not flip `eod_resolver_basis` back to `close_walk` to "match" the picker.** That would re-hide the wick in live settlement too — making the *loss* invisible rather than fixing the *choice*.

---

## 6. Built — Stages 1–5 (2026-10-08)

All five stages shipped. Tests: `tests/test_geometry_basis_harmonization.py`
(11 tests, all green) proves the contract end-to-end.

### Stage 1 — one `resolve_bracket` primitive, every consumer imports it

`settlement_contract.py` now owns the single definition of "did this bracket win":

| Function | Role |
|---|---|
| `resolve_bracket_path(...)` | **the reference walk** — one bracket vs one window, returns outcome/exit/ambiguous/basis |
| `bracket_envelope(...)` | per-bar (favourable, adverse) envelope — the grid-search shape |
| `resolve_bracket_from_envelope(...)` | resolve a bracket from a prebuilt envelope (vectorised grid) |
| `BASIS_INTRABAR` / `BASIS_CLOSE` | the sole knob that changes the answer |

Repointed consumers:
- **`tiered_atr.walk`** — gained `same_bar_policy`; new `walk_envelopes`,
  `build_envelopes`, `live_basis()`, `live_same_bar_policy()`.
- **`tiered_atr.select_tp_sl`** — accepts `envelopes=` (live basis) or `paths=`
  (close-walk parity); `dynamic_atr_pick` now builds envelopes on the live basis.
- **`atr_reopt`** (monthly **and** the 4h urgency that auto-applies clamps) —
  `_paths_for`/`eval_cell`/`solve_free` now measure on the live envelope.
- **`heavy_sweep`** — the grid still runs the Renko brick engine (a full trade
  lifecycle), so rewriting it was out of scope; instead the CHOSEN bracket is
  re-scored with the shared primitive on the live basis and the gap logged
  (`live-basis cross-check`) — observation only, no behaviour change.

**Proof** (`test_geometry_basis_harmonization.py`): `walk_envelopes`,
`resolve_bracket_path` and `resolve_bracket_from_envelope` each agree with the
live resolver **300/300** on random windows. `resolve_bracket_path` and
`walk_envelopes` reproduce `resolve_signal_outcome_ohlc` exactly.

### Stage 2 — one basis flag, honoured everywhere

- `eod_resolver.resolve_signal_outcome_ohlc` now **delegates** to
  `resolve_bracket_path` instead of running its own inline walk.
- The same-bar tie-break now honours `settlement_same_bar_policy` — it used to
  **hardcode SL-first while the flag read `tp_first`**. Set the flag to
  `sl_first` in both `config/feature_flags.yaml` and `feature_flags.py`
  `_DEFAULTS`, which (a) matches what live actually did and (b) removes the
  silent divergence. Live settlement is byte-preserved (the intrabar path only
  ever ran SL-first).

### Stage 3 — persist the ambiguity

Migration **204** adds `nt_signal_sim.ambiguous BOOLEAN` (nullable, additive)
+ a partial index. The resolver now writes it (both patch sites). Rows before
2026-10-08 read NULL = unknown; no fabricated backfill. This makes the 21.4%
same-bar population measurable per bucket.

### Stage 4 — re-derive geometry on the harmonized basis

Enabled by 1–3: the picker now optimises on the basis that settles, so the
next pick cycle re-derives the FX `sl_atr` band against the intrabar reality
(the tight 0.30–0.45 band was a close-walk artefact). The geometry replay
(roadmap id 71) must be re-run on this basis before any manual band change —
its earlier "TP 9–17 / SL 0.5–2" result is a close-walk figure.

### Stage 5 — the picker must agree with its own measurements

`dynamic_atr_consistency_mode` (off | shadow | **shadow** default):
`tiered_atr.check_pick_reachability_consistency()` compares the pick's walked
TP rate against the platform's measured `nt_bracket_reachability` bucket rate
(`fetch_bucket_measured_tp`). shadow logs; live rejects the pick (falls back to
the heavy pick); fail-open below 30 samples. Directly catches the GBPUSD case:
`walk_tp=0.48 vs measured_tp=0.05 (Δ0.43 > 0.20, n=400)`.

### Migration + flags

- `supabase/migrations/204_signal_sim_ambiguous.sql` — **applied** (idempotent).
- `settlement_same_bar_policy: "tp_first" → "sl_first"` (behaviour-preserving).
- `dynamic_atr_consistency_mode: shadow`, `dynamic_atr_consistency_tol: 0.20`.

### Deploy (2026-10-09 02:46 UTC)

Commit `8097208` pulled on the Studio (`/teamspace/studios/this_studio/noble-trader`,
which is the cwd of the running orchestrator), orchestrator restarted (PID 7707,
health 200). Verification:

- **Migration 204 confirmed on the real prod DB** (`pcvscowltlrxzgxjurcr`): the
  `ambiguous` column exists on `nt_signal_sim`. (The MCP Supabase tools point at
  a *different* project — always confirm through the Studio's own `.env.local`.)
  Zero non-null rows at deploy time because no resolution had run since the
  restart; the next hourly resolve populates it.
- **Process newer than the changed files** (`settlement_contract.py` /
  `tiered_atr.py` mtime 02:45:15 < process start 02:46:18) — the new code is live.
- Imports resolve with the right values: `live_basis=intrabar`,
  `live_same_bar_policy=sl_first`, `consistency=shadow/0.2`.
- Pre-sweep optimizer ran cleanly under the new process (02:50).

**The Studio's `config/feature_flags.yaml` carries runtime-tuned optimizer output**
(`atr_reopt.py` patches `dynamic_atr.by_symbol.<SYMBOL>` clamps). It was stashed,
the pull taken, then popped — the incoming hunk (new flags, ~line 499/844) and the
local hunk (by_symbol clamps, ~line 375/528/574/589) are disjoint, so both merged
with no conflict. Backup kept at `/tmp/ff_studio_backup_*.yaml`.

**Notable:** the live-tuned forex band on the Studio is already
`sl_atr [1.45, 1.57, 1.69]` — ~4× wider than the committed `[0.30, 0.35, 0.40]`,
i.e. the realtime optimizer had *already* widened the stop, consistent with the
Stage-4 prediction that the tight band was a close-walk artefact.

---

## 7. Evidence files

Ad-hoc scripts (scratch, not committed):
- `basis_replay.py` — same signals, close-walk vs intrabar envelope grid
- `isolate_divergence.py` — the 4-way basis × tie-break matrix (table in §2)
- `resolver_truth.py` — re-resolves stored episodes with the real resolver (83% agreement; the 17% gap is the tie-break + window anchoring)
- `verify_primitive.py` / `verify2.py` — the 300/300 primitive-agreement proofs

Key figures: picker +0.607 vs live −0.239 ATR/signal at TP1.65/SL0.35; same-bar-both-touched **21.4%**; live GBPUSD sell 7d WR 6.4% (n=204 episodes).
