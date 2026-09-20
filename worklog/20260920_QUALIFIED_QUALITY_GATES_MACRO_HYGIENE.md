# Qualified quality gates + macro evidence hygiene

> **Priority**: P0 quality · **Status**: A flipped live; B+C built (B shadow) · **Date**: 2026-09-20  
> **Trigger**: Pulse 2026-09-20 — 7d qualified **W8/L32/E20**, PnL ≈ **−$1.5k**; geo:tuning **shadow streak=8**; `macro_regime_mode=shadow` with **inverted** `nt_macro_edge_by_symbol`; mixed `computed_at` on macro edge.  
> **Parent**: `20260919_INTRADAY_GEOMETRY_TUNING_SCOPE.md`, `20260918_TIERED_ATR_GEOMETRY_SCOPE.md`, `20260919_HEAVY_GEOMETRY_PROMOTE_P0_P1_P2.md`

---

## Context (from pulse)

| Finding | Implication |
|---------|-------------|
| Qualified **count** OK, **quality** weak (AAPL 22q win~9%) | Gates reward **direction/EV**, not **reachability** |
| `p_win_bracket` often ~0 on qualified (USDCHF/US500) | Light path **not** gated on bracket win odds |
| Heavy has publish geometry gate; **light does not** | Same hole on the higher-volume path |
| `geo:tuning` shadow **streak=8** many symbols | Correct to enqueue; **live** needed for same-day heavy |
| `macro_regime_mode` shadow; labels mostly **neutral** | No live impact on calibration/reachability |
| `nt_macro_edge_by_symbol` **daily rebuild** but **mixed dates** (09-02…09-18) | Evidence table **not fully refreshed** |
| Macro-aligned often **worse** than opposed (AAPL aligned 12% WR, E[R] −0.64) | **Do not** flip macro live |

Macro is **not** a calibration/reachability lever today. Quality improves via **geometry + light-path gates**.

---

## Goals

1. **Stop publishing** light QUALIFIED signals that geometry says are unlikely to resolve as wins in 48h.  
2. **Activate** intraday scale + geometry tuning sweeps (already built, shadow).  
3. **Make macro edge trustworthy** (full rebuild) without flipping Kelly.  
4. Keep **qualified volume** possible when geometry is healthy — this is **quality**, not a kill-switch for the product.

---

## Workstreams

### A — Intraday geometry → live (already measured)

| ID | Work |
|----|------|
| A1 | Flip `geometry_intraday_scale_mode: live` — shadow logs show ATR contraction (BTC scale 0.5, metals ~0.8) |
| A2 | Flip `geometry_tuning_sweep_mode: live` — streak≥2 risk → `geometry_tuning_sweep` + single-symbol heavy (~6h cooldown) |
| A3 | Verify logs: `[geo:intraday]` applied bricks, `[geo:tuning] ENQUEUE`, emergency path runs, no raw-missing regressions |

**Not gated on Oct 5** for *measurement*; global `atr_anchored_mode=live` still separate.

### B — Light-path qualified quality gate (new)

| ID | Work |
|----|------|
| B1 | Flag `light_publish_geometry_gate`: `off \| shadow \| live` (default **shadow** first) |
| B2 | After all other light gates pass: if `geometry_risk_hat > dynamic_atr_risk_cap` **or** (`p_win_bracket` scored **and** `< min_p_win_bracket`) → `qualified=False` when **live**; when **shadow**, log `would_reject` + stamp `qualified_geometry_shadow=true` on sim |
| B3 | Fail-open: missing geometry / unscored bucket → **do not** reject (same as Batch C) |
| B4 | Tests: pass when risk ok; reject when risk high; shadow does not flip `qualified`; unscored does not reject |
| B5 | Scorecard: count `would_reject` rate — if &gt;~70% of would-be qualified, **tune cap** rather than strangling all output |

**Heavy already has** `heavy_publish_geometry_gate: live`. B **extends parity to light**.

### C — Macro edge hygiene (no live Kelly)

| ID | Work |
|----|------|
| C1 | Force **full rebuild** of `nt_macro_edge_by_symbol` (all symbols × alignments) and stamp `computed_at` on **every** upserted row |
| C2 | Optional: add `updated_at` vs `computed_at` split or document that `computed_at` = last row write |
| C3 | Fix / retry **`fetch_macro_regime` ReadTimeout** (timeouts → missing `macro_label` on sim) |
| C4 | **Keep `macro_regime_mode=shadow`** — no live flip until reliability-filtered n≥20 cells show aligned ≥ opposed on a majority of symbols |
| C5 | Roadmap note: macro review remains **2026-09-26** (per `nt_roadmap`); this worklog does **not** advance that date |

### D — Optional quality add-ons (after B)

| ID | Work |
|----|------|
| D1 | Heavy: require min recent sample or median `p_win_bracket` for QUALIFIED (AAPL-class 9% WR) |
| D2 | Per-symbol geometry risk floor override (e.g. gold vs FX) — only if B5 shows uneven reject rates |

---

## Flags (proposed)

```yaml
geometry_intraday_scale_mode: live      # after A1 decision
geometry_tuning_sweep_mode: live        # after A1/A2 decision
light_publish_geometry_gate: shadow     # B — flip after replay/logs
# unchanged: macro_regime_mode: shadow
# unchanged: p_win_bracket_mode: shadow (gate via B uses scored values when present)
```

---

## Proof / same-day checks

| Check | Pass bar |
|-------|----------|
| Unit tests B4 + geometry scale | Green |
| Full pytest | Green |
| After A2 live | `geometry_tuning_sweep` rows + single-symbol heavy within cooldown |
| After B live | Lower `long_lag_risk` share among **new** qualified; 7d W/L not worse on resolved |
| Macro C1 | All `nt_macro_edge_by_symbol.computed_at` within ~24h |

---

## Success metrics (2–4 weeks)

| KPI | Direction |
|-----|-----------|
| Qualified **resolved** win% | ↑ |
| Long-lag / expired on qualified | ↓ |
| Avg `p_win_bracket` on qualified | ↑ |
| 7d dual-report PnL | ↑ |
| Macro edge rows stale &gt;48h | → 0 |
| Light `would_reject` rate | Informative, not 100% |

---

## Non-goals

- Flip `macro_regime_mode` to live  
- Flip global `heavy_sweep_atr_anchored_bricks_mode` without Stage 1 / Oct 5  
- Raise qualified **count** as a target  
- Conviction-widening TP  

---

## Execution order

```text
1. A1/A2 — flip geometry scale + tuning sweep live (ops/config + Studio)
2. B1–B4 — light geometry gate shadow → tests
3. C1–C3 — macro edge full rebuild + regime fetch robustness
4. After 1–2 heavy/tuning cycles + B shadow logs → B live if reject rate sane
5. C4 — stay shadow; collect for 09-26 macro review
```

---

## Immediate build (on approval)

1. B: `light_publish_geometry_gate` + wiring + tests  
2. C1: rebuild path / CLI for `macro_edge` full refresh  
3. A: config flips only (no code) once you confirm deploy  

**Awaiting:** **build B+C** (and confirm **A** flags live in Studio).
