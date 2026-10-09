# Worklog: Dependency Map (geometry_incubation, first_passage) + Is "One Methodology, One Dataset" the Right Goal?

**Date:** 2026-10-09
**Scope:** (A) trace the full dependency graph of the two modules the chain triage called unwired; (B) assess — on logic and evidence, not preference — whether "one methodology, one dataset" is the correct target across the whole orchestrator chain, given that calibration and reachability measure *different quantities*.
**Related:** `worklog/20261009_ATR_GEOMETRY_SINGLE_SOURCE.md` (the harmonization this questions), `docs/diagrams/orchestrator_chain.html`, `docs/diagrams/harmonization_audit.html`, `worklog/20261008_GEOMETRY_BASIS_DIVERGENCE.md`

---

# Part A — Dependency map

## A.1 `geometry_incubation.py`

**Purpose:** the forward-test gate that should sit between a heavy-sweep geometry pick and `config/brick_params.yaml`. Today `write_brick_params()` takes an in-sample 90-day grid result straight to the live YAML on the next tick — no out-of-sample check. This module is that missing check.

### Dependency graph

```
                         ┌──────────────────────────────────────────┐
                         │  heavy_sweep.py  (daily, 17:06 UTC)      │
                         │  after the Renko grid pick               │
                         └───────────────┬──────────────────────────┘
                                         │ calls 2 of 5 public fns
                    ┌────────────────────┼─────────────────────┐
                    ▼                    ▼                     ▼
        start_incubation()      fetch_baseline_pct_expired()   (skip_direct_write
        → INSERT nt_geometry_    → reads nt_brick_params_state    decides whether the
          incubation row           + nt_bracket_reachability      YAML write happens)
                    │
                    │  ★ reconcile_incubations()  ← THE PROMOTION ENGINE
                    │    reads nt_geometry_incubation (status=incubating)
                    │    → _fetch_forward_resolved (nt_signal_sim, resolved_at>=started_at)
                    │    → compute_forward_stats()  (pure)
                    │    → evaluate_promotion()     (pure)
                    │    → PATCH status / write_brick_params() when live
                    │
                    └──► ★ NO CALLER ANYWHERE IN THE REPO ★
```

### Every dependency, classified

| Dependency | Kind | Status |
|---|---|---|
| `heavy_sweep.start_incubation()` call | **inbound, live** | ✅ called (line 1225) |
| `heavy_sweep.fetch_baseline_pct_expired()` | **inbound, live** | ✅ called (line 1223) |
| `reconcile_incubations()` | **inbound** | ❌ **no caller** — not in the scheduler, not in any job, not in any script |
| `nt_geometry_incubation` (table, migration 165) | store | ✅ exists |
| `nt_signal_sim` (forward rows) | data | ✅ read by `_fetch_forward_resolved` |
| `nt_brick_params_state` | data | ✅ read for baseline |
| `nt_bracket_reachability` | data | ✅ read for baseline bucket |
| `brick_params.write_brick_params()` | **outbound** | ✅ called, but only from the dead reconcile path (and heavy_sweep's own write) |
| `feature_flags` (4 getters) | config | ✅ read inside reconcile |
| `dynamic_tier_optimizer._compute_geometry_incubation()` | **parallel impl** | ⚠️ see A.3 |

### Live consequence (verified on prod)

```
nt_geometry_incubation:  434 rows
  status superseded : 420
  status incubating :  14     ← rows accumulate, never decided
  n_resolved > 0    :   0     ← the forward stats were NEVER computed
  last_checked_at set:  0     ← reconcile_incubations NEVER ran
```

**The module is half-wired: it *records* candidates and never *evaluates* them.** Every candidate is frozen at `started_at`; the 14 "incubating" rows will never promote or reject. `start_incubation` is a write with no matching reader.

## A.2 `first_passage.py`

**Purpose:** promote `walk()`'s arithmetic — P(TP before SL) — from a *geometry selector* to an *EV-blend probability source*, replacing the withdrawn `p_markov_hold_n`.

### Dependency graph

```
   tiered_atr.EXPIRY_BARS ──┐
   kairos_rank.freshness_score ──┤
   kairos_rank.type_score ───────┤──► first_passage.py
                                 │      first_passage()        (pure, P(TP before SL))
                                 │      transition_carry()     (pure, D5 reinforcement)
                                 │      first_passage_score()  (pure, shrunk read)
                                 │      evaluate_rows()        (offline helper)
                                 │
                                 └──► ★ imported by fp_eval.py ONLY (offline) ★
```

| Dependency | Kind | Status |
|---|---|---|
| `tiered_atr.EXPIRY_BARS` | import | ✅ live |
| `kairos_rank.freshness_score` / `type_score` | import | ✅ live (Kairos-proven findings) |
| `fp_eval.first_passage_arm()` | inbound | ⚠️ **offline evaluator only** |
| `scripts/kairos_fp_eval.py` | inbound | ⚠️ **manual script only** |
| EV blend slot | **intended outbound** | ❌ **never wired** |
| `first_passage_score()` | outbound | ❌ no external reference |
| `evaluate_rows()` | outbound | ❌ no external reference |
| Live sweep / orchestrator | inbound | ❌ none |

### Why it is unwired — by decision, with evidence

| Bar | Requirement | Result |
|---|---|---|
| B1 | beat the base-rate control | **FAIL** — skill −0.0691 vs base rate 0.7041 |
| B3 | positive skill ≥ 2/3 of symbols | **FAIL** — 3/14 |
| B5 | overlay adds skill | **FAIL** — ΔD −0.0265 |
| — | **1 of 5 bar items passes** | **KILL CRITERIA MET** → option F |

First-passage measured **below the naive base rate**. It is unwired because the evidence said not to wire it — the correct outcome, unlike `geometry_incubation` where a needed gate is missing.

### The asymmetry between the two modules

| | `geometry_incubation` | `first_passage` |
|---|---|---|
| Built for | a **missing** out-of-sample gate | a **proposed** replacement source |
| Unwired because | **omission** — `reconcile_incubations()` has no caller | **decision** — kill criteria met |
| Effect today | every candidate silently frozen | no effect (never wired, never claimed) |
| Correct action | **wire it** | **leave it** (or delete) |

This is the key distinction the earlier triage collapsed into one bullet ("built but unwired"). They are opposite situations.

## A.3 Third defect found while mapping: two incubation engines, one of them reading a non-existent column

`dynamic_tier_optimizer._compute_geometry_incubation()` (line 321) is a **second, independent** incubation evaluator. It duplicates `evaluate_promotion()`'s logic — and reads a column that does not exist:

| It reads | Table has | Verdict |
|---|---|---|
| `row.get("n_forward_resolved")` | `n_resolved` | ⚠️ **always 0** — wrong column name |

Consequence: `if n_resolved < 20: keep_incubating` **always holds**, so the optimizer's incubation tier can never emit anything but `keep_incubating`. That is why all 28 `geometry_incubation` rows in `param_store` are inert, and it is a *different* failure from the missing reconcile caller — the same "two implementations of one concept" defect this week's work keeps finding.

---

# Part B — Is "one methodology, one dataset" the right goal?

## B.1 The thesis, stated precisely

The harmonization worklog's implicit target: every consumer in the chain should resolve outcomes the same way, over the same window, from the same data. Part A of the earlier audit already showed windows differ (10d / 45d / 90d) and filters differ.

**The question is whether that is a defect or a design.** Answer: **it is mostly design — the thesis is right for one axis and wrong for the other.**

## B.2 The distinction that resolves it

There are two different things the phrase "one methodology, one dataset" conflates:

| | **Resolution semantics** | **Estimation population** |
|---|---|---|
| Question | *"What happened?"* | *"What is the rate?"* |
| Must be identical because | a win must mean the same thing everywhere, or two consumers disagree about the *same trade* | — |
| Must be allowed to differ because | — | different questions need different samples |

- **Resolution semantics = MUST be one.** If the resolver says `sl_hit` and the picker says `tp_hit` for the same signal, the platform is internally contradictory. This is what the geometry-basis work fixed, correctly.
- **Estimation population = MUST be allowed to differ.** A 7-day calibration window and a 90-day reachability window are answering different questions; forcing them equal destroys one of them.

## B.3 Why calibration ≠ reachability — the metric axes are different

The user's observation is exactly right, and it is the crux. Verified from the code:

| | **Calibration** (`calibration_enforcement` / `eod_correction`) | **Reachability** (`bracket_reachability`) |
|---|---|---|
| **Quantity** | `bias = mean(predicted_p_win) − realized_win_rate` | `pct_hit_tp / pct_hit_sl / pct_expired` |
| **Unit of observation** | one **episode** (deduped signal) | one **bracket-bucket** `(symbol, vol_tier, distance_atr_bucket, signal)` |
| **Predicts** | *confidence accuracy* — "are our probabilities honest?" | *geometric reachability* — "does this bracket distance get touched?" |
| **Conditioned on** | symbol × side (and tier) | symbol × vol_tier × **ATR-distance bucket** |
| **Window** | **7 days** (`EOD_BIAS_WINDOW_DAYS`), half-life decayed | **all resolved history** (rolling table rebuild) |
| **Failure it catches** | overconfidence (predicted 0.7, realised 0.4) | unreachable geometry (TP 4 ATR away never touched) |
| **Output consumer** | p_win shrink, kelly shrink | p_win_bracket, deadline tier, incubation baseline |
| **Sample size seen live** | tens of episodes | **USDCAD 4+/all n=1478** vs **XAUAUD 0.5-1/sell n=25** |

**These are not the same measurement at different scales — they are different measurements.** Concretely, live:

```
USDCAD  4+  all :  n=1478   pct_hit_tp 1.69%   pct_expired 79.4%
XAGUSD  4+  all :  n=633    pct_hit_tp 32.2%   pct_expired 13.1%
```

Same bucket *label* (`4+`), two entirely different populations. Calibration would call both "the same geometry band"; reachability shows they are not comparable at all. **Forcing one dataset across them would be the error, not the fix.**

## B.4 The logical test: what breaks if you force one dataset?

| Forced unification | What breaks |
|---|---|
| Same **window** (say 10d) for calibration | bias computed on ~tens of episodes → noise; the 7d half-life decay exists precisely because 10d+ dilutes a regime shift |
| Same **window** (say 90d) for reachability | buckets stop reflecting current vol regime; the whole point of `vol_tier` bucketing is regime-locality |
| Same **unit** (episode vs bucket) | impossible — a bucket aggregates many episodes by construction |
| Same **conditioning** | calibration needs side; reachability needs ATR-distance. Neither can drop its axis without losing its meaning |

Every unification fails a concrete test. The thesis is **falsified** as stated for the estimation layer.

## B.5 Where the thesis IS right — and the honest scorecard

| Layer | Should be one? | Evidence |
|---|---|---|
| **Resolution semantics** (what "hit" means) | ✅ **YES — done** | 3/4 sites now use `resolve_bracket_path`; heavy Renko cross-checked |
| **Same-bar tie-break** | ✅ **YES — done** | one flag, read everywhere |
| **Param authority** (who owns a value) | ✅ **YES — done** | `config/*` authoritative; optimizer calls the picker |
| **Estimation window** | ❌ **NO — deliberately varied** | 7d calibration / 10d picker / 45d reopt / 90d reachability, each justified |
| **Estimation population/filters** | ❌ **NO — deliberately varied** | sweep-light signals vs all-resolved rows vs bucket-aggregated |
| **Metric definition** | ❌ **NO — must differ** | bias (confidence) ≠ pct_hit (reachability) |

**Correct target:** *one resolution semantics, many estimation populations.* The harmonization work already achieved the right half; the earlier audit's "not harmonized" findings on windows/filters are **not defects** — they are the necessary variety of an estimation layer.

## B.6 What the chain actually needs (the real remaining gaps)

Reframed from "harmonize everything" to what is genuinely wrong:

1. **`reconcile_incubations()` has no caller** — a needed gate is missing. *Wire it (scheduler job).*
2. **`_compute_geometry_incubation` reads `n_forward_resolved`** — wrong column; always 0. *Fix to `n_resolved`, or delete the duplicate and call the real evaluator.*
3. **Two incubation engines** — same single-source-of-truth defect as the ATR picker. *Collapse to one.*
4. **`first_passage` unwired** — correct by decision. *Leave; document the kill.*
5. **`brick_params.yaml` is a second geometry store** alongside the `dynamic_atr` tier — *genuinely ambiguous ownership; this one IS a harmonization candidate.*

None of items 1–5 require forcing calibration and reachability onto one dataset.

## B.7 Bottom line

**The "one methodology, one dataset" thesis is half-right, and the half that is wrong is the half the phrase emphasises.**

- **"One methodology"** — correct, and done, for *resolution semantics*. Extending it to *estimation* would be wrong.
- **"One dataset"** — incorrect as a chain-wide goal. Calibration (confidence accuracy, 7d, episode-level) and reachability (geometric reachability, all-history, bucket-level) measure different quantities; the divergence is the design working, not failing.

The real defects are three specific wiring bugs (Part A) plus one genuine ownership ambiguity (`brick_params.yaml` vs the `dynamic_atr` tier). The fix is to repair those, **not** to unify the estimation layer — which would degrade calibration and reachability both.

---

# Part B (build) — Recommendation: retire `brick_params.yaml` as a *geometry authority*, keep it as a *rendering cache*

**Question put:** should `dynamic_atr` be the source of truth, retiring `brick_params.yaml`?

**Answer: yes on the principle, no on the deletion — and the distinction is load-bearing.** `dynamic_atr` should become the sole **geometry authority**. `brick_params.yaml` should stop being an authority but must **not** be deleted, because it is not only a geometry store: it is also the **brick-rendering + retention key** for the Renko engine. Deleting it breaks those consumers. The correct end state is *one authority, one derived cache*.

## B.8 What `brick_params.yaml` actually is — three roles, not one

Verified from the consumer map (`.gitignore:138` tracks it; 6 production readers):

| Role | Consumers | What it needs |
|---|---|---|
| **1. Bracket geometry** | `orchestrator._load_brick_params` (light), `heavy_sweep` (writes), `geometry_incubation` (writes), `renko/router.py:2078` (Discord embed) | `sl_bricks`, `tp_bricks` |
| **2. Brick rendering scale** | `renko_retention.py:203` (prune key), `renko/router.py` `get_brick_size()` | `brick_size` |
| **3. Per-symbol defaults** | `load_brick_params` fallback `(0.5, 3, 6)` | both |

**Role 2 is the trap.** `brick_size` is not a bracket parameter — it is the **Renko brick width**, and `renko_retention` uses it to decide which `nt_renko_bricks` rows are still current (a wrong value wiped 924k rows on 2026-10-01 — see the memory note). A naive "delete brick_params.yaml" removes the retention key and the Discord embed's price basis.

## B.9 Why `dynamic_atr` is the right authority — the argument is structural, not preference

The `dynamic_atr` pick *is* the bracket, in ATR units, computed from the live path on the live basis. `brick_params.yaml` holds the same decision **quantised into integer brick counts** — a lossy re-encoding:

```python
# tiered_atr.DynamicAtrPick.to_bricks — explicitly "Lossy vs exact ATR"
brick = self.sl_atr * atr / sl_bricks          # brick_size derived FROM the pick
tp_bricks = max(1, round(self.tp_atr / self.sl_atr * sl_bricks))
```

`brick_size` and `tp_bricks` are **functions of** `sl_atr`/`tp_atr` + a chosen `sl_bricks`. So `brick_params.yaml` is a *downstream encoding* of the `dynamic_atr` pick — it cannot be the authority for something it is derived from. Today the dependency runs backwards: the heavy sweep computes a pick, encodes it to bricks, writes the YAML, and the light path reads the encoding. That is the same "consumer re-derives / reads a lossy copy" defect as the geometry-basis and ATR-tier work.

**The authority must be the thing that is not derived from anything else.** That is the pick (`tp_atr`, `sl_atr` in ATR units). Confirmed.

## B.10 The blocker: `sl_bricks` is a free parameter, so the pick alone is not sufficient

`to_bricks(atr, sl_bricks)` takes `sl_bricks` as an **input** — it is chosen by the heavy grid, not by the pick. So `(tp_atr, sl_atr)` does not uniquely determine `(brick_size, sl_bricks, tp_bricks)`; you must also fix `sl_bricks` (or equivalently the brick width).

This is why a straight "make `dynamic_atr` authoritative, delete the YAML" fails: the light path needs three integers, and the pick supplies two ATR ratios plus a brick count that only the heavy grid knows.

**Resolution — and it is the same shape as every other fix this week:** make the pick carry the missing input. Add `sl_bricks` (the chosen brick count) to the persisted `DynamicAtrPick` JSON. Then `to_bricks(atr, pick.sl_bricks)` fully determines all three integers from the authority, and `brick_params.yaml` becomes pure cache.

## B.11 Recommended path (staged, shadow-first, reversible)

| Stage | Change | Risk | Reversible |
|---|---|---|---|
| **1** | Persist `sl_bricks` in the `DynamicAtrPick` JSON (tier `dynamic_atr`). One field, additive. | none | yes |
| **2** | Add `brick_params.load_from_store(symbol, atr)` — build `(brick_size, sl_bricks, tp_bricks)` from the tier via `to_bricks`, **falling back to the YAML** when the store is cold. | low (fail-open) | yes |
| **3** | Point `orchestrator._load_brick_params` at `load_from_store` behind a new flag `brick_params_source: yaml \| store` (default `yaml`). | low | yes (flag) |
| **4** | In `store` mode, keep **writing** the YAML from the store on each pick (so `renko_retention` + `renko/router` stay correct) — but mark it a *cache*: the write is now derived, never an input. | low | yes |
| **5** | Flip `brick_params_source: store`. YAML becomes a derived artifact, exactly like `param_store.sqlite`'s Supabase mirror. | medium | yes (flag) |
| **6** | *(optional)* Retire the heavy-sweep grid's authority over `sl_bricks` once the pick's own value proves stable. | higher | — |

**Net effect:** `dynamic_atr` (param_store tier) = the authority; `brick_params.yaml` = a derived cache refreshed from it, still present for the Renko engine and the Discord embed. Nothing is deleted; the arrow direction is reversed.

## B.12 What NOT to do, and why

- **Do not delete `brick_params.yaml`.** It is the retention key for `nt_renko_bricks` and the Discord embed's price basis. Deleting it is the 2026-10-01 wipe waiting to happen again.
- **Do not make the store authoritative while `dynamic_atr` is empty.** Right now the picker returns `None` for all 14 symbols (every candidate is negative in the first half — verified live). A store-authoritative light path with an empty store must fall back to the YAML, or the light sweep loses its geometry entirely. This is exactly why stage 2 is fail-open and stage 5 is a flag.
- **Do not skip stage 1.** Without `sl_bricks` in the pick, the store cannot reconstruct the brick encoding, and any "store mode" would silently drift from the YAML.
- **Do not force calibration/reachability onto the same dataset** while doing this (Part B.5) — the geometry authority question and the estimation-population question are independent.

## B.13 Honest status

**BUILT 2026-10-09.** The plan below changed when the build started: the store
**already** carried the complete triple. `param_store` tier `brick_state`
(key `latest`) holds `{brick_size, sl_bricks, tp_bricks, atr_at_write}`, written
by `geometry_risk.persist_brick_state_local` from the same heavy-sweep call that
writes the YAML (`heavy_sweep._persist_brick_params_to_yaml`). So **stage 1 from
the sketch was unnecessary** — no new field on `DynamicAtrPick`, no schema
change. The `brick_state` tier is an authority-in-waiting; the light path simply
did not read it.

What shipped:

| Stage | Built |
|---|---|
| **1** | *(not needed)* — `brick_state` already holds the resolved triple |
| **2** | ✅ `brick_params.load_from_store()` — local-only read of the triple |
| **3** | ✅ `load_brick_params_authoritative()` + flag `brick_params_source: yaml \| store` (default `yaml`); `orchestrator._load_brick_params` routes through it |
| **4** | ✅ *(already true)* — the heavy sweep dual-writes YAML + store, so the YAML stays current as cache with no change |
| **5** | ⏳ flag flip — **deferred**, see below |
| **6** | ⏳ retire the grid's authority over `sl_bricks` — not started |

**Why the flip is deferred, and it is the same reason as before:** `store` mode
only becomes *meaningful* once the store holds values the YAML does not — i.e.
once the ATR picker produces picks. Today it returns `None` for all 14 symbols
(every candidate is negative in the first half, verified live in
`20261009_ATR_GEOMETRY_SINGLE_SOURCE.md` §8), and the `brick_state` tier is
written by the heavy sweep's own YAML path, so store and YAML hold the *same*
values. Flipping now would be a no-op at best. The machinery is in place and
fail-open; flip `brick_params_source: store` when a symbol's pick diverges from
its heavy-grid brick encoding — that is the moment the store becomes the truer
authority.

Tests: `tests/test_brick_params_source.py` (10) — flag default + fail-safe,
store read/cold/partial, `yaml` mode byte-identical, `store` mode reads store,
`store`+cold falls back, and a guard that the light path routes through the
authoritative read.

