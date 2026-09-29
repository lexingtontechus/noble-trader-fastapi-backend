# Geometry band rework — dynamic ATR picker (2026-09-29)

**Status:** BUILT (local) · `dynamic_atr_mode` stays **shadow**  
**Parent:** `worklog/kairos/20260924_KAIROS_V3_SCOPE.md` §14.5 ·  
`reports/20260926_geometry_replay.md` · 2026-09-28 pulse HOLD on live flip

---

## 1. Why

09-28 pulse pre-read of `dynamic_atr_mode` live flip:

- Hybrid **promote is healthy** (exp% 15.8 vs control 25.4; pnl_bricks +0.14).
- But shadow **geometry is still too wide**: picker proposed `tp=6–24 ATR · sl=1`,
  not the replay’s best region.
- Replay (8,252 signals, 87.3% outcome parity): **TP 9–17 ATR · SL 0.5–2 ATR**
  is positive both halves (+0.45…+0.56 ATR/signal). Dynamic-ATR feasible set
  was only **+0.08 ATR** and its **TP 0.3–2** band *excluded* that region.

**Prescribed next step (now this worklog):** rework the geometry **bands /
percentages** before any live flip. Not a flag flip on hybrid gains alone.

## 2. Root cause in the picker

`feature_flags.yaml` `dynamic_atr`:

| | Before | After |
|---|---|---|
| `tp_atr` | `[1.5, 2, 3, 4, 6, 9, 12, 17, **24**]` | `[9, 12, 15, 17]` |
| `sl_atr` | `[**1.0**, 1.5, 2, 3]` | `[**0.5**, 1.0, 1.5, 2.0]` |
| clamps | none | `tp∈[9,17]`, `sl∈[0.5,2]` |

`select_tp_sl` maximises lookback expectancy under `max_expiry`. On a trending
10d window the **widest TP (24)** wins even when it sits outside the
positive-both-halves region the replay identified. SL floor 1.0 never tried
**0.5**, the tight-stop half of the best pair.

## 3. Design

1. **Tighten the candidate grid** to the replay band (small lattice inside it:
   TP 9/12/15/17 × SL 0.5/1/1.5/2).
2. **Hard clamps** in `select_tp_sl` (`tp_atr_min/max`, `sl_atr_min/max`) so
   a future grid edit cannot silently pick outside the band.
3. **Unchanged:** both-half positivity, `max_expiry`, `min_rr`, cost, cache,
   `dynamic_atr_mode` (still **shadow**). No live geometry change.

## 4. Files

| File | Change |
|---|---|
| `regime_platform/core/feature_flags.py` | default grid + clamp keys |
| `config/feature_flags.yaml` | live grid + clamps |
| `regime_platform/sweep_orchestrator/tiered_atr.py` | `select_tp_sl` clamps; `dynamic_atr_pick` passes them |
| `tests/test_tiered_atr.py` | `TestBandClamp` + flag assertions |

## 5. Verification

```
pytest tests/test_tiered_atr.py   # 15 passed (incl. TestBandClamp)
```

Expected shadow log after next heavy (still `mode=shadow`):

```
[dynamic:atr] <SYM> mode=shadow would set tp=9..17ATR sl=0.5..2ATR ...
```

— **never** `tp=24ATR` again.

## 6. Live-flip bar (unchanged, after this rework)

1. Shadow picks land in TP 9–17 / SL 0.5–2.
2. 48–72h light outcomes on promoted vs control beat the **+0.08 ATR** prior
   on expectancy (not just exp%).
3. Then consider `dynamic_atr_mode: live`. Kill bar: exp% flat/up **or**
   closed expectancy worse → stay shadow.

## 7. Decision log

| Date | Decision |
|---|---|
| 2026-09-28 | HOLD live flip; prescribe band rework (pulse). |
| 2026-09-29 | Build grid + clamps. Mode remains shadow. |
