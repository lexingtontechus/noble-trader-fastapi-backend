# Geometry coherent flow — redefined (rev 2)

**Date:** 2026-10-12
**Type:** Architecture scope (config-resolution triage + target design)
**Supersedes:** `docs/scopes/20261012_GEOMETRY_SINGLE_SOURCE_COHERENT_FLOW.md` (rev 1) — its `NO_TRADE` outcome is **withdrawn**.
**Decisions baked in (user):** ATR over bricks (settled 2026-10-12). **No `NO_TRADE` gate** — geometry is a *parameter applied to a promoted signal*, not a gate; qualification is the gate. **On any `None`/null → RECOMPUTE fresh**, never fall back to old params (a stale fallback is a corrective process run over an untested value). **One config resolution.** Collapse the gate sprawl.
**Status:** Triage complete. Redesign for review.

---

## 0. Plain terms — what "envelope" means

An **envelope** is the picker's compressed memory of what price did after each past signal. For every historical signal it stores, for each of the next 48 hourly bars, **two numbers**: the **favourable** excursion (how far price ran *with* the trade, in ATR) and the **adverse** excursion (how far *against*). So an envelope is an array shaped `(n_signals × 48 bars × 2)`.

The picker then "walks" a candidate TP/SL through it: did the favourable side reach TP before the adverse side reached SL, within 48 bars? That scores a bracket without storing raw OHLC.

Why it exists: a **close-path** walk (one price per bar) cannot see a wick, so a tight stop *looks* survivable when in reality price wicked through it intrabar. The envelope carries the intrabar extremes, so it can see the wick — that was the whole point of the 2026-10-08 basis fix. One line: **envelope = per-signal, per-bar favourable/adverse ATR excursion; the picker resolves a bracket against it.**

---

## 1. Triage — config resolution is doing different things in different places

### 1.1 One function, three argument shapes, three different answers

`get_dynamic_atr_config(asset_class, symbol)` resolves `by_symbol.<SYMBOL> → by_class.<key> → global` — but **which layers apply depends on the caller's arguments**:

| call site | call | layers applied | effect |
|---|---|---|---|
| `dynamic_tier_optimizer.py:218` (per-symbol pick) | `(class, symbol)` | by_symbol ✅ by_class ✅ global | symbol's own band |
| `dynamic_tier_optimizer.py:182` (**pooled** pick) | `(class, None)` | **by_symbol SKIPPED** by_class ✅ global | class/global band |
| `heavy_sweep.py:1117` (heavy path) | `(class, symbol)` | by_symbol ✅ | symbol's own band |
| `atr_reopt.py:193` (drift compare) | `(class, symbol)` | by_symbol ✅ | symbol's own band |
| `atr_reopt.py:545` (vol gate) | `(None, symbol)` | **by_class SKIPPED** | symbol or global |

**Consequence (verified live):** the *pooled* pick for BTCUSD is clamped against the **global** band (`tp_atr_max 20.0`), so it stored **13.0 ATR** — violating BTCUSD's own `by_symbol` band (`tp_atr_max 8.80`). Same class of violation on US500 (stored 2.93 vs band 12.5–14.0) and XAGUSD (6.39 vs 7.5–8.3). The *per-symbol* pick for the same symbol would be clamped correctly — so **the same symbol resolves to two different bands depending on which call path runs.**

### 1.2 FOREX is defined twice, differently

- `dynamic_atr.by_symbol.FOREX` → `tp_atr [1.65, 1.71, 1.75, 1.80]`
- `dynamic_atr.by_class.forex` → `tp_atr [1.65, 1.71, 1.80]`

Two blocks, one concept, different grids. `by_symbol.FOREX` wins for a forex *symbol*; `by_class.forex` wins for the *pooled* call. They were meant to be the same band.

### 1.3 A third definition lives in code

`feature_flags._DEFAULTS["dynamic_atr"]["by_class"]` defines `commodity/equity/forex/crypto` bands that **differ from the YAML** (e.g. code `by_class.forex` is `tp [1,2,3,4] / sl [2,3,4]` — SL wider than TP). Today the YAML's `by_class` shadows them, but they are a latent second source: delete the YAML key and behaviour silently changes.

### 1.4 The light path reads a *store*, not the config

`orchestrator.py:4156–4183`: the light path computes **brick first**, then reads a persisted pick (`read_pick_from_store(symbol, max_age_hours=6.0)`), and if the store is empty/stale it **keeps the brick**. So the applied geometry is decided by *store occupancy and age*, not by the config or a live solve. Nine of fourteen symbols have no store row → they trade brick. This is the "old params" fallback the redesign must remove.

---

## 2. Triage — the gate sprawl (why it's "circular death")

Ten independent conditions can each nullify the geometry or silently switch it to a different system. Each is a separate failure point; none is a single decision:

| # | gate | where | on failure |
|---|---|---|---|
| 1 | flag/atr guard (`_atr_light != off and atr>0`) | orchestrator | brick |
| 2 | store hit + age ≤ 6h | orchestrator | brick |
| 3 | per-symbol raw signal count ≥ min_signals | tiered_atr | → pooled |
| 4 | pooled raw signal count ≥ min_signals | tiered_atr | → brick |
| 5 | envelope count ≥ min_signals | select_tp_sl | None |
| 6 | per-candidate: `min_rr`, 4 clamp bounds, `max_expiry`, both-halves e1/e2 | select_tp_sl | candidate skipped |
| 7 | `vol_ratio` band | tiered_atr | None → brick |
| 8 | consistency gate (`dynamic_atr_consistency_mode`) | tiered_atr | pick rejected → brick |
| 9 | walk-forward gate (`dynamic_atr_walkforward_mode`) | tiered_atr | pick rejected → brick |
| 10 | store-write clamp gate | write_pick_to_store | not stored → brick |

The result: the geometry a signal trades is a function of *which gates happened to pass on which tick*, with **brick as the universal sink**. That is the incoherence.

---

## 3. Redesign — one flow, no gates on the geometry path

### 3.1 One config authority

**`geometry_params(symbol)`** — resolves the symbol's own band ONCE, identically for every caller. Pooling changes the **evidence window**, never the band. Concretely:

- Every caller calls `geometry_params(symbol)` — no more `(asset_class, symbol)` shapes that silently skip layers.
- **One FOREX band**, defined once (delete the `by_symbol.FOREX` / `by_class.forex` duplication; keep a single source and alias to it).
- Delete the shadowed code `_DEFAULTS by_class` (or make it the only source and remove the YAML copy — one place, not two).
- The pooled solve passes the **inheriting symbol's** band, so a pooled pick can never violate the symbol's clamps.

### 3.2 One solve that always returns geometry

**`solve_geometry(symbol, scope)`** — the only TP/SL producer:

- Evidence scope widens `per-symbol → class → global` (a bigger window of the *same* measurement). This is the only "fallback", and it is **dynamic — a fresh recompute**, never a stored/old value.
- Candidate quality filters (`min_rr`, expiry, both-halves, consistency, walk-forward) run **inside** the solve as **candidate filters**: a rejected candidate is simply not chosen; the solve moves to the next candidate. **They never null the result.**
- If no candidate clears the strict bar, the solve returns the **best-expectancy in-band bracket computed from the current window** (with its expectancy and the reason recorded) — a real, current, in-band geometry. It **never returns `None`**, so nothing downstream can fall back to brick.

### 3.3 Null → recompute, never a stale fallback

The rule, applied everywhere:

> **A `None`/null/miss/stale value triggers a fresh recompute from current data. It never resolves to a stored, old, or brick parameter.**

So the light path stops reading a possibly-stale store and keeping brick. A short-TTL cache is fine (performance), but **a miss recomputes**; it does not fall through to a different system.

### 3.4 Geometry is a parameter, not a gate — no `NO_TRADE`

- The geometry layer **always produces** TP/SL for the candidate.
- The **qualification chain remains the single gate**. A qualified signal is promoted and the dynamic geometry is **applied** to it.
- There is **no `NO_TRADE`** from the geometry layer, and no geometry-level publish gate. (Rev 1's `NO_TRADE` is withdrawn.)

### 3.5 The flow

```
signal candidate
  └─ geometry_params(symbol)            # ONE config — the symbol's own band, always
  └─ solve_geometry(symbol, scope)      # ONE solve — always returns an in-band bracket
        candidate filters run INSIDE (min_rr/expiry/both-halves/consistency/walk-forward)
        rejected candidate → next candidate   (never nulls the result)
        none clear the strict bar → best-expectancy in-band bracket, reason recorded
  └─ apply TP/SL to the signal          # geometry is a PARAMETER
  └─ qualification chain                # the ONLY gate
  └─ publish if qualified
  └─ reachability rebuilt from the applied geometry   # measurement follows selection
```

**Invariants this restores:**
- One config, one solve, one geometry — by construction, not by convention.
- No `None` propagates → no stale fallback → no "corrective process over an untested value".
- No `NO_TRADE`; qualification alone decides promotion.
- Gates filter *candidates*, not *results* — so they cannot sink the geometry to a parallel system.
- The reachability table measures the geometry the signal actually trades.

---

## 4. What this does NOT fix (stated plainly)

- **No edge.** The grid dump (rev 1 §1) shows forex is −0.32 to −0.39 ATR/signal in both halves and commodity is split-regime. A coherent flow makes the geometry honest and current; it does not manufacture expectancy. Qualification still decides whether anything is promoted.
- **Volume.** Per-symbol solves need ~80 resolvable paths; the book yields 2–3. Class pooling is the honest mitigation (wider evidence window), and it is now a *parameter of the one solve*, not a separate system.

---

## 5. Sequencing (one lever, era-clean, verifiable against the grid dump)

- **G1 — One config authority.** `geometry_params(symbol)` used by every call site; pooled passes the inheriting symbol's band; collapse the duplicate FOREX band; retire the shadowed code defaults. *Acceptance:* BTCUSD resolves to its own band (max 8.80) on **every** path, pooled included.
- **G2 — One solve, always returns geometry.** Fold the candidate filters inside `solve_geometry`; remove `None` returns; delete the brick TP/SL derivation (`_compute_trade_signal` keeps direction only). *Acceptance:* every symbol, every tick, resolves to an in-band bracket.
  **DONE 2026-10-12** (`worklog/20261012_GEOMETRY_G2_G3_AND_FOREX_BAND_REPLAY.md`):
  `select_tp_sl` returns the best-expectancy in-band bracket when nothing clears
  the strict both-halves bar (`allow_best_effort`, default on); `dynamic_atr_pick`
  replaces every `return None` with `_band_center_pick(cfg)` — the operator's
  deliberate mid-band value. `tests/test_geometry_always_returns.py`.
  *Remaining:* the brick TP/SL derivation in `_compute_trade_signal` is still
  present as the pre-apply value (G3 overrides it on every tick when the light
  mode is on), so deleting it is a follow-up, not a blocker.
- **G3 — Null → recompute.** Replace the light path's store-read-else-brick with resolve-else-recompute. *Acceptance:* a cold/empty store still yields current dynamic geometry, never brick.
  **DONE 2026-10-12:** `_process_symbol` now calls `dynamic_atr_pick(...)`
  (store-hit or recompute) instead of `read_pick_from_store` + brick fail-open.
  Pinned by a wiring assertion in `tests/test_geometry_always_returns.py`.
- **G4 — Reachability rebuilt from the applied geometry.** *Acceptance:* `nt_bracket_reachability` buckets the shipped ATR bracket.

No step adds a gate; each removes one.

---

## 6. Files / lineage

- `regime_platform/core/feature_flags.py` — `get_dynamic_atr_config` (§1.1), `_DEFAULTS["dynamic_atr"]["by_class"]` (§1.3).
- `regime_platform/sweep_orchestrator/tiered_atr.py` — `dynamic_atr_pick`, `select_tp_sl`, `to_bricks`, `write_pick_to_store`, gates #3–#10.
- `regime_platform/sweep_orchestrator/dynamic_tier_optimizer.py:175–233` — pooled fallback (config with `symbol=None`).
- `regime_platform/sweep_orchestrator/orchestrator.py:4145–4183` — brick-first + store-read light path.
- `config/feature_flags.yaml` — `dynamic_atr.by_symbol.FOREX` vs `by_class.forex` (§1.2).
- Evidence: `nt_dynamic_tier_state`, `nt_signal_sim`, `nt_bracket_reachability`; grid dump in rev 1 §1.
