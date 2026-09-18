# TimesFM z-map — definitive fix for p≈0.5

> **Priority**: P1 EV source · **Status**: **zmap_live** flipped 2026-09-18 + short invert · **Date**: 2026-09-17  
> **Evidence**: Studio `orchestrator.log` via `ssh noble-trader-unified` (confirmed live access).  
> Probes: XAGUSD rel_change=+0.12% → p=0.503; BTCUSD +0.052% → 0.501; EURUSD +0.009% → 0.500.  
> `flat=False` — TimesFM **does** forecast; **k-sigmoid on raw %** cannot resolve sub-percent moves.

## Root cause (definitive)

```
p = σ(k · rel_change),  k=10
rel_change ≈ 0.0001 … 0.001  →  p ≈ 0.500 … 0.503
```

Raising k is not a fix: k must be ~10²–10³ for 0.1% moves, which then makes 1% moves look certain.

## Fix

Per-symbol **z-map** (Gaussian CDF of standardized forecast error):

```
z = rel_change / max(σ_symbol, σ_floor)
p = Φ(z) = 0.5·(1 + erf(z/√2))
```

- Rolling σ of `rel_change` per symbol (local SQLite, warm start optional)
- `σ_floor` + `min_n` (default 30) — below that, keep sigmoid and log `zmap=insufficient`
- **Modes** (`timesfm_map_mode` in feature_flags.yaml):
  - `sigmoid` — today’s behavior
  - `zmap_shadow` — log both, feed **sigmoid** to EV (default on flip)
  - `zmap_live` — feed **z-map** p_timesfm to EV
- Probe always logs both p_sigmoid and p_zmap

## Non-goals

- No TimesFM model/horizon change
- No gate-threshold change in this worklog

## Proof

- Unit tests: Φ math, σ floor, min_n, mode wiring
- Live: Studio probe lines show `p_zmap` varying with |rel_change|/σ, not pinned at 0.5
- Flip to `zmap_live` only after shadow lines look sane for a few sweeps
