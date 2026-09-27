# Changelog — EV engine / qualification: drop Kelly as a publish gate

**Date:** 2026-09-27  
**Repo:** `noble-trader-fastapi-backend`  
**Scope:** EV engine + 7-gate qualification (light sweep)  
**Status:** live (with Studio pull) · variable Kelly **shadow only**

---

## What changed

| | Before | After |
|---|---|---|
| **`kelly_min_ok` gate** | `effective_kelly >= MIN_KELLY` (historical-mu sizer) required to publish | **Removed from quality gates** |
| **Historical Kelly** (`PositionSizer` mu/sigma²) | Vetoed ~61% of directional scans (size 0) | Still computed and stored (`kelly_f`, `effective_kelly`) for **research only** |
| **Publish quality** | 7 gates including Kelly | **p_win, EV, regime_conf, TimesFM, signal_qualifiable** (+ optional context alignment) |
| **Sizing (live)** | Unchanged (agent / paper path) | Unchanged |
| **Sizing (research)** | — | **Variable binary Kelly shadow stamps** (`kelly_binary_f`, `kelly_context_mult`, `kelly_effective_shadow`) |

## Why

1. **Two scorers disagreed.** p_win/EV (model blend) vs historical-mu Kelly (recent price path). Gold printed `p_win 0.75` and `kelly = 0`, then failed `kelly_min_ok`.
2. **Quantity cut, quality not up.** 61% Kelly-zero share blocked publishes without a measured quality gain (pulse 2026-09-27).
3. **Kairos:** regime-age knobs are disproved (V2/V4). Size variation belongs in **event-time transition context** (fresh / `dir_flip`), not age — implemented as **shadow** multipliers only.

## Binary vs historical (one line)

- **Binary Kelly:** size from *this* signal’s `p_win` and TP/SL payoff — matches the model that already passed quality gates.  
- **Historical Kelly:** size from recent return mean/variance — answers “has price been friendly?” and zeros strong model signals.

## Variable Kelly (shadow)

```text
f*  = (p_win·b − (1−p_win)) / b     b = TP/SL
m   = context(event_window, transition_type, vol_tier)   # never age
size_shadow = f* × 0.5 × m
```

Columns on `nt_signal_sim` (migration **174**): `kelly_binary_f`, `kelly_context_mult`, `kelly_effective_shadow`.  
**Never** read back into qualification or live size until a separate promote bar (same as `kairos_rank_score`).

## Risk / rollback

| Risk | Mitigation |
|---|---|
| More publishes (Kelly no longer vetoes) | Other gates still bind (p_win 0.55 hard floor 0.50, EV, regime_conf). Cooldown 15m. |
| p_win overconfidence → oversized binary Kelly | Fraction 0.5 + context mult ≤ 1.35; **shadow only** |
| Live sizing drift | Live size path untouched |

**Rollback:** restore `"kelly_min_ok": effective_kelly >= MIN_KELLY` in `gate_results` (orchestrator).

## Related

- `worklog/20260927_PULSE_ROADMAP_DECISIONS_AND_QUALITY_FORK.md` §1 (plain language)  
- `worklog/20260927_QUALIFIED_VS_SUPPRESSED_PUBLISH_PATH_REDESIGN.md` (publish path)  
- `worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md` (T1 / dir_flip)  
- Roadmap: `kelly_geometry_quality_fork`
