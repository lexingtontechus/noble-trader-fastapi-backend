# Intraday geometry tuning — stop waiting on 24h heavy sweeps

> **Priority**: P0 · **Status**: T1–T4 built (shadow defaults); commit after suite · **Date**: 2026-09-19  
> **Problem**: Brick geometry only rewrites on **daily heavy sweep** (~24h). Light sweep (15 min) **reads** YAML but never **fixes** brackets.  
> **User language**: prefer **tuning** over “emergency.”  
> **Existing path**: `_execute_emergency_heavy_sweeps` + 4h `eod_correction` — today driven by **calibration bias**, not geometry risk.

---

## Why 24h feels broken

| Path | Cadence | Changes SL/TP? | Trigger |
|------|---------|----------------|---------|
| Light sweep | 15 min | **No** — uses current `brick_params.yaml` | cron |
| Heavy sweep (daily) | ~24h | **Yes** — grid search writes YAML | cron |
| “Emergency” heavy | when bias action fires | **Yes** | calibration bias / degeneracy |
| Intraday EOD correction | 4h | No geometry write — writes actions | cron |

**Gap:** We can **see** bad geometry in 15 minutes (`geometry_risk`, `tp_atr`, long-lag) but **cannot correct** it until the next heavy cron or a bias-driven emergency. Bias ≠ geometry: a symbol can be **well-calibrated on direction** and still publish **unreachable TP**.

---

## Three levers (do all — different speed / risk)

### Lever A — Intraday **vol-drift tuning** (fastest, no full grid)

**Where:** light sweep `_process_symbol`, after loading `brick_params` + live ATR.

```
vol_ratio = ATR_now / ATR_at_write   # from nt_brick_params_state
if mode=live and vol_ratio outside [0.7, 1.3]:
    effective_brick_size = brick_size_yaml * clip(vol_ratio, 0.5, 2.0)
    # SL/TP = entry ± effective_brick_size * sl/tp_bricks  (same brick counts)
```

| | |
|--|--|
| Latency | **Next light tick (~15 min)** after ATR moves |
| Risk | Low if **never widens** past yaml when already 4+ ATR (cap factor) |
| Default | `geometry_intraday_scale_mode: shadow` |
| Does not | Re-optimize brick counts / RR |

**This is the real “intraday fix” for stale weekly/daily bricks when vol expands/contracts.**

### Lever B — Geometry **tuning sweep** (rename + rewire emergency)

New action + trigger, reusing `_execute_heavy_sweep_for_symbol` / existing heavy runner:

| Field | Spec |
|-------|------|
| Name | `geometry_tuning_sweep` (alias of emergency path; keep old name in DB for compatibility) |
| Triggers | Light: `long_lag_risk` or `geometry_risk_hat > cap` **N consecutive ticks**; or 4h correction pass if scorecard expiry / `tp_atr` median high |
| Scope | **Target symbol(s) only** — not full 14-symbol sweep |
| Grid | Same as heavy; **force ATR-anchored candidates eligible** for this run (evidence promote / temporary mode) |
| Cooldown | Separate from bias emergency — e.g. **6h** per symbol (`geometry_tuning_cooldown_hours`) |
| Cadence | Can fire **anytime** after light detects + cooldown clear — **not** wait for 22:00 UTC |

Write `nt_eod_actions` / `nt_geometry_tuning_actions` with `action=geometry_tuning_sweep`.

### Lever C — 4h pass becomes geometry-aware

On `eod_correction` intraday (and nightly): if `nt_brick_params_state` + sim show high long-lag / `tp_atr` &gt; 2 for symbol → enqueue **geometry_tuning_sweep** (not only bias `emergency_heavy_sweep`).

---

## What we are **not** doing

| Non-reason | Note |
|------------|------|
| Waiting 24h “for evidence” | Measurement is already live; **correction cadence** is the issue |
| Conviction-widening TP on light ticks | Increases long-lag |
| Full heavy grid every 15 min | Cost + overfit; only **drift scale** intraday + **targeted** tuning sweep |
| Replacing daily heavy | Keep daily; tuning is a **fast path** |

---

## Flags / config (proposed)

```yaml
geometry_intraday_scale_mode: shadow   # off | shadow | live
geometry_tuning_sweep_mode: shadow     # off | shadow | live
geometry_tuning_cooldown_hours: 6
geometry_tuning_min_consecutive: 2     # light ticks over risk cap
# reuse dynamic_atr_risk_cap for trigger
```

---

## Implementation slices

| ID | Work | Proof |
|----|------|-------|
| **T1** | Light: read `nt_brick_params_state.atr_at_write`; compute `vol_ratio`; shadow log `[geo:intraday]` | unit + log |
| **T2** | Light: if live, scale **brick_size only** for trade_signal / sim (capped) | unit: identical when ratio≈1 |
| **T3** | Light + 4h pass: detect geometry risk → write `geometry_tuning_sweep` action | tests |
| **T4** | Orchestrator: on action, run **single-symbol** heavy + ATR candidates in-pool | reuse existing emergency dispatch |
| **T5** | Scorecard: `tp_atr` median / long-lag after tuning cycles | live table |

**Cadence after ship:**  
- Vol-drift scale: **15 min** (light)  
- Tuning sweep: **within hours** of a bad-geometry detect (cooldown permitting)  
- Daily heavy: still runs; redundant when tuning is healthy  

---

## Go-live order

1. **T1 shadow** (see vol_ratio + would-be scale on live light ticks)  
2. **T3/T4 shadow** (actions logged, heavy not forced)  
3. After one clean day of shadow: `geometry_intraday_scale_mode=live`  
4. Then `geometry_tuning_sweep_mode=live` for symbols over risk cap  
5. Keep `atr_anchored_evidence_mode` / roadmap for **global** ATR mode  

---

## Success

| KPI | Target |
|-----|--------|
| Time from “risk detected” → new brick_params | **≤ few hours**, not 24h |
| `tp_atr` after tuning | **↓** |
| Long-lag share on qualified | **↓** |
| No extra 4+ publishes from intraday scale | **0** when ratio already wide |

---

## Immediate next build (on approval)

1. T1 + T2 (light vol-drift shadow → live) — **biggest 24h win**  
2. T3 + T4 geometry tuning sweep rewire (reuse emergency infrastructure)  
3. Tests + full pytest  

**Awaiting:** build order choice (all T1–T4 vs light-scale only first).
