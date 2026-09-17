# TimesFM probe + cadence/aggression clarity

> **Priority**: P1 observability · **Status**: built · **Date**: 2026-09-17  
> **Trigger**: Post-deploy sanity check — `p_timesfm` present on all sweeps but range only 0.496–0.501; user asked about direction-strength SL/TP tiering and 15-min cadence.

## Findings (data)

| Check | Result |
|---|---|
| Light sweep | **15 min** (confirmed live marks + `*/15` default) |
| TimesFM wired | Yes — 100% `p_timesfm` not-null |
| TimesFM useful | **No** — pinned ~0.5 |
| Aggression → SL/TP | **No** — geometry is `brick_params` only |

## Shipped

1. **`[timesfm:probe]` log** — forecast, last, rel_change, k, pre-clamp sigmoid, clamped p, flat-flag warning.
2. **AGENTS.md** — light sweep every 15 min (was stale “5 min”).
3. **`EVEngine.edge_tier()`** + docstring: aggression is order-style, not conviction; does not tier SL/TP.
4. **ACTIVE_MODES.md** — cadence table + aggression note.

## Next (if probe shows flat forecast)

Root-cause TimesFM service / horizon / last_price input. Do not treat aggression as SL/TP tiering until a separate conviction×vol geometry scope (parked thesis).

## Deploy

Redeploy Studio; grep logs for `timesfm:probe` and `flat=True` after next sweep.
