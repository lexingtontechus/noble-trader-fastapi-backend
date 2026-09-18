# Tiered ATR geometry — close the 4+ bucket gap

> **Priority**: P0 economics · **Status**: shadow path built (dynamic_atr_mode=shadow); live flips still nt_roadmap 2026-10-05 · **Date**: 2026-09-18  
> **Reference**: `docs/Tiered_Trading_Targets.md` (idealized prior art — not authoritative)  
> **Evidence**: Live pulse 2026-09-17 — ~100% recent brackets in **`4+` ATR**; avg `p_win_dir≈0.50` vs **`p_win_bracket≈0.10`**; 7d qualified PnL negative.  
> **Related**: `zmap_live` TimesFM (probability honesty) does **not** fix geometry.

---

## Reachability reframe (canonical language)

Platform metrics and gates are designed around a **short-horizon** trade: price hits **TP or SL within 48h** (settlement deadline). That is the book we *score*.

| Class | Definition (operational) | Not by choice |
|-------|--------------------------|---------------|
| **Short-horizon resolve** | Outcome `tp_hit` / `sl_hit` **within 48h** | — what scorecard win%/PnL/gates assume |
| **Long-horizon / geometry-lag** | Did **not** resolve in 48h (`expired`); if left open, often resolves in **~72h–10d+** (or never cleanly) | **Not** a strategy “swing” — **symptom of wide brackets vs ATR and the deadline** |

**Implication:** A high rate of long-horizon (expired) signals is **unmanaged tail risk** created by **geometry**, not evidence of a deliberate long strategy. The platform is **short-horizon calibrated**; long-horizon drift is **off-model**.

So:

```
p_resolve_48h  = P(tp_hit or sl_hit within 48h)     # short-horizon capacity
p_long_lag     = 1 - p_resolve_48h                  # geometry risk share
```

**Long-lag % must drive ATR/geometry risk** — not be ignored because “expired ≠ win/loss” in calibration.

---

## Problem

SL/TP are `entry ± brick_size × bricks` from **weekly** `brick_params.yaml`, often **several ATRs** wide. Within **48h** most of those brackets never complete a **short-horizon** resolve. Signals then live in the **long-lag** class (72h–10d+) with **no** designed exit — risk created by geometry.

| Tiered doc idea | Action |
|-----------------|--------|
| ATR / vol-class brick scaling | **Build on** Phase 3b ATR-anchored candidates |
| Partial scale-outs / trail | Out of scope here |
| Time-stops / regime exits | 48h deadline exists; long-lag is the **gap**, not a missing trail |
| “80–100% win” tables | Do not trust |
| Conviction-widening TP when EV high | Defer — increases long-lag share |

## Goal

Reduce **long-lag (geometry) share** and move `tp_distance_atr` toward **0.5–2×**:

| KPI | Meaning |
|-----|---------|
| `% short-horizon resolve (≤48h)` | ↑ — platform operating as designed |
| `% long-lag / expired` | ↓ — this **is** the geometry risk metric |
| p75 / p90 hours-to-resolve on resolved set | ↓ |
| Share of brackets in **4+ ATR** | ↓ |
| `p_win_bracket` on qualified | ↑ |
| Dual-report PnL | ↑ closed **and** all-resolved |

---

## Dynamic ATR design — risk inputs (user #1 + review)

**Rule:** Never publish / persist a bracket whose **predicted long-lag risk** exceeds a cap unless a human override. Dynamic ATR is **risk-informed**, not only “fit the grid Sharpe.”

### Required inputs (feed tiered / dynamic brick_size)

| # | Factor | Why it matters | Source (today / to add) |
|---|--------|----------------|-------------------------|
| **R1** | **Long-lag rate** `p_long_lag` by `(symbol, vol_tier, distance/ATR bucket)` | Direct measure of “unmanaged >48h” share | `nt_bracket_reachability.pct_expired` |
| **R2** | **Time-to-resolve tail** p75/p90 hours | 48% expire is bad; same expire with p90=200h is worse | reachability `p75_hours`/`p90_hours` |
| **R3** | **Proposed `tp_distance_atr` / `sl_distance_atr`** at write time | Geometry *before* it is live | heavy sweep candidate metrics (**add** `tp_distance_atr`) |
| **R4** | **Vol drift** `ATR_now / ATR_at_brick_params_write` | Week-old bricks + new vol → accidental long-lag | live ATR vs YAML write stamp (**add** `atr_at_write`) |
| **R5** | **48h MTM of expired cohort** | Long-lag isn’t free — signed drift at deadline | `nt_signal_sim` expired `pnl_dollars` |
| **R6** | **Session / market hours** | Equities weekends burn wall-clock 48h; crypto does not | `market_hours` + calendar-aware deadline (optional later) |
| **R7** | **Regime / vol_tier at capture** | Same bucket ≠ same path (already in reachability key) | `regime` → vol_tier |

### Design formula (spec — Phase C/A wiring)

```
# Risk score for a candidate bracket at selection / light-sweep write
long_lag_hat = lookup_p_long_lag(symbol, vol_tier, bucket(tp_distance_atr))
               # if unscored: conservative prior, e.g. max(system_median, 0.4)

geometry_risk = long_lag_hat
                * (1 + w_t * f(p90_hours / 48))
                * vol_drift_factor(ATR_now / ATR_at_write)

# Dynamic ATR action
if tp_distance_atr > target_max_atr:          # e.g. 2.0
    prefer narrower ATR-anchored candidate
if geometry_risk > RISK_CAP:                  # e.g. 0.35 long-lag
    reject pick / fall back to best ≤ cap candidate
    # do NOT widen TP because ev_per_dollar is high
```

**Feeds:**

1. **Tiered ATR (static tiers):** asset-class + vol-tier **default** brick multiples (crypto/equity/FX/metal) — **priors**, not destiny.  
2. **Dynamic ATR (per pick):** same tiers **adjusted** by R1–R5 so a symbol with high long-lag gets **narrower** geometry until R1 improves.

### What must NOT drive ATR up

| Anti-factor | Reason |
|-------------|--------|
| High `p_win_dir` / aggression “passive” | Direction ≠ reachability |
| In-sample Sharpe on wide brackets | Deadline-unaware trap |
| “Need RR ≥ 1.5” alone | Can force unreachable TP |

If RR floor conflicts with reachable TP, **shrink SL and/or accept lower RR** — do not stretch TP into 4+ ATR.

---

## Phases (same-day proof each; off/shadow/live)

### Phase A — ATR-anchored candidates → evidence → live

| Step | Work |
|---|---|
| A1 | Grid includes ATR multiples when mode ∈ {shadow, live} |
| A2 | Multi-window replay: with vs without ATR candidates — **report long-lag % + dual PnL**, not only Sharpe |
| A3 | Live if long-lag ↓ and no catastrophic symbol regression |

### Phase B — Reachability pick + **long-lag risk cap**

| Step | Work |
|---|---|
| B1 | Shadow `reachability_aware_pick` + new **geometry_risk** (R1,R2) on pick |
| B2 | Replay table: long-lag share, p90 hours, expectancy |
| B3 | Live when risk-capped pick beats baseline on long-lag and PnL |

### Phase C — Dynamic ATR hygiene (light path)

| Step | Work |
|---|---|
| C1 | Persist `atr_at_write` + `brick_params_written_at` on YAML/symbol state |
| C2 | Every `nt_signal_sim`: `tp_distance_atr`, `sl_distance_atr`, `geometry_risk_hat` |
| C3 | If `geometry_risk_hat > CAP` → log `long_lag_risk=true` (diagnostic) |
| C4 | Heavy write path: **only** persist candidates with risk ≤ CAP when `dynamic_atr_mode=live` |

### Phase D — Conviction

| Step | Work |
|---|---|
| D1 | Conviction may **narrow** TP/SL when long-lag high — never widen |

## Non-goals

- Broker trail / partial exits  
- `p_win_bracket` live gate until A–C evidence  
- Treating “long positions” as a **strategy** — they are **geometry-lag artifacts**  

## Proof harness

| Metric | Script / view |
|--------|----------------|
| Long-lag % by bucket | reachability + scorecard `expiry_pct` |
| Dual PnL | `v_symbol_signal_scorecard*` |
| Dir vs bracket | `p_win_dir` / `p_win_bracket` |
| Replay A2/B2 | extend deadline-aware / ATR backfill scripts — **add long-lag column** |

## Success

| KPI | Target |
|-----|--------|
| Long-lag / expired % (qualified 7d) | ↓ toward low teens or better |
| 4+ ATR share of new signals | ↓ majority &lt;2 ATR |
| Avg `p_win_bracket` (qualified) | ↑ |
| Dual-report PnL | ↑ |
| `geometry_risk` on published picks | mostly under CAP |

## Immediate next build (when approved)

1. **C2** — `tp_distance_atr` / `geometry_risk_hat` on sim capture + migration.  
2. **A2/B2 replay** with long-lag % as first-class KPI.  
3. **B3/C4** — risk-capped pick + ATR write once replay passes.

---

## Build log (2026-09-18) — not committed until full verification

| Piece | Path |
|-------|------|
| Geometry risk module | `regime_platform/sweep_orchestrator/geometry_risk.py` |
| Migration | `supabase/migrations/156_nt_signal_sim_geometry_risk.sql` |
| Sim capture | `tp_distance_atr`, `sl_distance_atr`, `long_lag_hat`, `geometry_risk_hat`, `long_lag_risk` |
| Lookup | `lookup_long_lag` from reachability (`n_stored≥3`) |
| Replay | `scripts/replay_tiered_atr_geometry.py` |
| Tests | `tests/test_geometry_risk.py` |

### Deploy / verify checklist (before commit)

1. Run full `pytest tests/`  
2. Apply migration **156**  
3. Redeploy Studio  
4. Confirm new sim rows carry geometry fields; `[geometry:risk]` logs when flagged  
5. Run replay (fixture + `--live`)  
6. Then commit/push with zmap_live + short invert + this geometry batch  

### Not built yet (B3/C4/D)

- Risk-capped heavy-sweep pick write  
- `atr_at_write` persistence on YAML  
- Conviction-narrow-only mode  

## Build log — tiered ATR shadow (2026-09-18)

| Piece | Path |
|-------|------|
| Tier priors + risk-capped pick | `regime_platform/sweep_orchestrator/tiered_atr.py` |
| Flags | `dynamic_atr_mode: shadow`, `dynamic_atr_risk_cap: 0.35` |
| Brick write provenance | migration `157_nt_brick_params_state.sql` + `_persist_brick_params_to_yaml(..., atr=...)` |
| Heavy-sweep wiring | Shadow `[tiered:atr]` log after reachability; **live pick only if `dynamic_atr_mode=live`** |
| Tests | `tests/test_tiered_atr.py` |

### Verification

```
pytest tests/ → 1078 passed, 21 skipped
```

### Roadmap respect

| Action | Allowed now? |
|--------|--------------|
| Shadow logs + nt_brick_params_state | **Yes** |
| Flip `dynamic_atr_mode` live (risk-capped pick) | After shadow evidence / replay |
| Flip `heavy_sweep_atr_anchored_bricks_mode` / reachability live | **nt_roadmap 2026-10-05 Stage 1** |

### Deploy

1. Apply migration **157**  
2. Redeploy Studio  
3. Grep `[tiered:atr]` on next heavy sweep  
4. Commit after Studio confirms logs (user gate)

## Operator glossary (one page)

| Term | Meaning |
|------|---------|
| **Short-horizon signal** | Resolved W/L within 48h — what the platform is calibrated for |
| **Long-lag signal** | Open/unresolved past 48h; often 72h–10d+; **geometry risk**, not a chosen swing |
| **Reachability** | Can TP/SL be hit in-window given distance/ATR + vol tier? |
| **Long-lag %** | Share of signals failing short-horizon resolve — **inputs dynamic ATR risk** |
| **`p_win_bracket`** | P(short-horizon win) ≈ p_resolve × P(win\|resolve) |
| **Tiered ATR** | Vol/asset-class defaults for brick multiples |
| **Dynamic ATR** | Per-symbol adjustment using long-lag risk, vol drift, proposed distance |


## Success criteria (program)

| KPI | Target direction |
|---|---|
| % signals in 4+ ATR | **↓** (majority &lt;2) |
| 7d expiry (qualified) | **↓** toward low teens |
| Avg `p_win_bracket` on qualified | **↑** |
| Dual-report PnL | **↑** closed and all-resolved |
| `bracket_gate_shadow` reject rate | meaningful but not ~100% |

## Immediate next build (when approved)

1. `tp_distance_atr` column + sim capture (Phase C2 — cheap measurement).  
2. ATR-anchored multi-window replay script + table (Phase A2).  
3. Worklog status: built A2 evidence, then flag flip.
