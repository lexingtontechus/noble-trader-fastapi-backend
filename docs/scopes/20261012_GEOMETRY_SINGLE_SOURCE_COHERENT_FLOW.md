# Geometry single source of truth — one coherent flow, not a brick/ATR dual path

**Date:** 2026-10-12
**Type:** Architecture scope (empirical diagnosis + target design)
**Trigger:** Reachability red flag; the ATR pick does not reach the signal for 9/14 symbols; the "fallback" is a whole second geometry system. User directive: **no bypasses and patches — a contiguous and coherent flow.**
**Status:** **SUPERSEDED 2026-10-12 by `docs/scopes/20261012_GEOMETRY_COHERENT_FLOW_REDESIGN.md` (rev 2).** The §1 grid-dump evidence and §2 diagnosis stand; the §3 design is withdrawn (its `NO_TRADE` outcome and its store-based fallback are rejected). Read rev 2 for the design.
**Relevant:** `worklog/20261012_GEOMETRY_HARMONY.md`, `worklog/20261012_POOLED_ATR_AND_SCORECARD_FILTER.md`, `worklog/20261012_REACHABILITY_GEOMETRY_FAILURE_DIAGNOSIS.md`, `config/feature_flags.yaml` (`dynamic_atr`), `regime_platform/sweep_orchestrator/tiered_atr.py`.

---

## 1. Verified: why the ATR pick is absent for forex / metals

**It is not the count gate.** Running `dynamic_atr_pick` against live data, the envelope counts are healthy and clear `min_signals` (60) comfortably:

| scope | envelopes | ≥ min_signals? |
|---|---|---|
| forex (pooled) | 1,171 | ✅ |
| commodity (gold+silver, pooled) | 752 | ✅ |
| XAUUSD (per-symbol) | 186 | ✅ |
| XAGUSD (per-symbol) | 187 | ✅ |
| equity (pooled) | 268 | ✅ |

**The pick is absent because `select_tp_sl` finds NO candidate bracket with positive expectancy, net of cost, in BOTH halves of the window.** The full candidate grid (live, intrabar basis, `sl_first`, `min_rr=1.5`, `cost_atr=0.1`, `max_expiry=0.2`):

### FOREX pooled — every candidate negative in BOTH halves
| tp | sl | RR | e1 (first half) | e2 (second half) | expiry |
|---|---|---|---|---|---|
| 1.65 | 0.30 | 5.50 | −0.330 | −0.323 | 0.000 |
| 1.71 | 0.35 | 4.89 | −0.362 | −0.345 | 0.000 |
| 1.80 | 0.40 | 4.50 | −0.380 | −0.387 | 0.000 |

All 9 candidates sit at **−0.32 to −0.39 ATR/signal in both halves**, expiry 0.000 (the tight 0.30–0.45 ATR stop is hit almost every time). Uniformly, structurally negative — no pick is possible, and none *should* be.

### COMMODITY pooled — a regime split, positive in only ONE half
| tp | sl | RR | e1 | e2 | expiry |
|---|---|---|---|---|---|
| 8.0 | 2.0 | 4.00 | **−1.339** | +0.294 | 0.057 |
| 13.0 | 2.0 | 6.50 | **−1.656** | +0.565 | 0.069 |
| 13.0 | 5.0 | 2.60 | −2.432 | +0.126 | 0.234 (expiry fail) |

The wide candidates are **strongly negative in the first half and positive in the second** — the book's edge appeared only in the recent half. The both-halves bar correctly refuses to promote a bracket that only earned on the half it was observed on.

### XAUUSD per-symbol — both halves negative
All 9 candidates e1 ≈ −0.90…−1.01, e2 ≈ −0.33…−0.44.

**Conclusion:** the solver is *correct*. At current volume the book has no edge at any geometry in the grid. The honest output is `None`. This is exactly what `20261012_GEOMETRY_HARMONY.md §5` predicted: *"Both brick and ATR geometry lose at ~10–19% WR on this book. Harmonizing the measurement makes the two systems agree; it does not create edge."*

---

## 2. The structural defect — why this is not a patch

The absent pick is honest; the *architecture around it* is not. Six distinct incoherences, each a source of the "too many conditions and bugs":

1. **Two geometry systems, one shadow bridge.** `_compute_trade_signal` (brick: `entry ± brick_size × tp/sl_bricks`) and `select_tp_sl` (ATR: `entry ± tp/sl_atr × atr`). Brick is the de-facto live source (40/40 in the harmony replay; 9/14 symbols today, verified 100% brick-match). The ATR path is a **parallel system** that usually produces nothing.

2. **The "fallback" is a whole second system, not a parameter.** Fail-open to brick is not "the ATR pick with a wider scope" — it is a *different TP/SL derivation* with a different bucket, a different reachability read, and a different EV. That is the definition of a non-harmonized flow.

3. **Config resolution is scope-dependent and inconsistent.** `get_dynamic_atr_config(class, symbol)` resolves `by_symbol → by_class → global`. The pooled call passes `symbol=None`, so it **skips `by_symbol`** — the pooled pick is clamped against a *different band* than the symbol inherits. Verified: BTCUSD's stored pick is **13.0 ATR, violating its own `by_symbol` band (`tp_atr_max: 8.80`)**; US500's stored 2.93 violates its band (12.5–14.0); XAGUSD's 6.39 violates its band (7.5–8.3). The clamp gate runs against the wrong band.

4. **Eight independent gates, each its own silent bypass.** raw `min_signals` → envelope `min_signals` → `min_rr` → `max_expiry` → both-halves expectancy → 4 clamp bounds → `vol_ratio` band → consistency gate → walk-forward gate. Each can independently return `None` and route to brick. There is no single place that answers "what geometry does this symbol trade right now, and why."

5. **Store vs live divergence.** The pick store holds picks from *earlier* solves (5 symbols). The *live* solve returns `None` for **every** class right now. So the shipped geometry is neither the current solve nor uniform across symbols — it is a stale artifact of whenever a solve last succeeded.

6. **Measurement follows the shipped (fallback) geometry.** `nt_bracket_reachability` buckets whatever shipped — i.e. the brick. So the tight-bucket red flag *measures the fallback*, and the ATR band that was widened is never measured at all.

---

## 3. Target design — one geometry authority

**Principle: exactly one function produces a symbol's TP/SL, and everything downstream reads it.** Brick is demoted to a *display quantization* of that geometry, never a source. The "fallback" becomes a **wider evidence scope within the same solve**, never a different system.

```
resolve_geometry(symbol) ──────────────────────────────────────────────┐
  1. ONE config: the symbol's OWN band (by_symbol → by_class → global). │  ← the clamp scope
     Pooling changes the EVIDENCE window, never the clamp band.         │
  2. ONE solve over [per-symbol → class-pooled → global-pooled],        │
     ALL candidates clamped to (1).                                     │
  3. Outcome is ALWAYS one of:                                          │
       • a bracket (tp_atr, sl_atr, expectancy, n, source)              │
       • NO_TRADE  (explicit stand_by, reason recorded)                 │
     — never a silent switch to a parallel geometry.                    │
  4. The chosen bracket IS the signal's TP/SL. Its bucket, reachability,│
     p_win_bracket, EV, and the reachability TABLE are all built from it.│
└──────────────────────────────────────────────────────────────────────┘
```

### Concrete changes

- **One config resolution.** A single `geometry_config(symbol)` used by *every* call site (per-symbol, pooled, heavy, light, reachability). The pooled solve passes the **inheriting symbol's** band, not `None`. This alone fixes the BTCUSD/US500/XAGUSD band violations.
- **One solve, scope as a parameter.** `solve(scope=per_symbol|class|global)` — the only difference is the evidence window size. No second derivation.
- **Brick is a projection.** `DynamicAtrPick.to_bricks()` already exists; make it the *only* brick computation, used for the Renko display, and delete `_compute_trade_signal`'s independent `brick_size × bricks` TP/SL.
- **`NO_TRADE` is a first-class result.** When no bracket clears the bar, the flow returns `NO_TRADE` with the reason (which gate failed, on which half) — and the sweep publishes nothing, rather than shipping a −EV bracket it cannot justify. This is the `stand_by` archetype, already modeled in the R0 registry.
- **One reachability.** `nt_bracket_reachability` is rebuilt from `resolve_geometry`'s output, so measurement and selection read the same geometry by construction (the harmony goal, now with no parallel path to diverge).

### Why this is coherent and not a patch

- It removes the *second system* rather than adding a guard to it.
- It makes the fallback a **parameter of the one solve** (scope), so "dynamic and aligned to the ATR pick" is literally true — there is no other geometry to fall back to.
- Every downstream consumer (bucket, reachability, `p_win_bracket`, EV, resolver, gate) reads one value, so the "one number, read the same way everywhere" invariant holds structurally, not by convention.

---

## 4. What this does NOT fix (stated plainly)

- **The no-edge problem.** The grid dump above shows the book is −0.32 to −0.39 ATR/signal on forex in *both* halves, and split-regime elsewhere. A coherent flow does not create edge — it makes the absence of edge **visible and actionable** (an honest `NO_TRADE` / a suppressed book) instead of masked by a fallback that ships −EV brackets.
- **Volume.** Per-symbol solves need ~80 resolvable paths; the book yields 2–3. Pooling to class is the honest mitigation, but it assumes intra-class geometry homogeneity (forex majors yes; metals-vs-equity no).

---

## 5. Sequencing (one lever, era-clean)

- **G1 — One config resolution.** `geometry_config(symbol)` used by every call site; pooled passes the inheriting symbol's band. Fixes the band violations. Verifiable now (BTCUSD 13.0 → rejected/8.x).
- **G2 — One solve, scope parameter; brick as projection.** Collapse the two derivations; `to_bricks` becomes the only brick path.
- **G3 — `NO_TRADE` as a first-class outcome.** Sweep publishes nothing when no bracket clears; reason recorded on `nt_signal_sim`.
- **G4 — Rebuild reachability from the resolved geometry.** Measurement follows selection.

Each step is independently verifiable against live data (the grid dump is the acceptance harness). No step is a bypass.

---

## 6. Files / lineage

- `regime_platform/sweep_orchestrator/tiered_atr.py` — `dynamic_atr_pick`, `select_tp_sl`, `asset_class_key`, `to_bricks`.
- `regime_platform/sweep_orchestrator/dynamic_tier_optimizer.py::_refresh_dynamic_atr` — pooled fallback (config with `symbol=None`).
- `regime_platform/sweep_orchestrator/orchestrator.py` — `_compute_trade_signal` (brick), `dynamic_atr_light_mode` bridge.
- `config/feature_flags.yaml` — `dynamic_atr.by_symbol` / `by_class` bands.
- Evidence: `nt_dynamic_tier_state`, `nt_signal_sim`, `nt_bracket_reachability`; grid dumps in this doc §1.
