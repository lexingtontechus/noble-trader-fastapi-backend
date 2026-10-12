# Reachability red flag — where the widened/harmonized ATR geometry fails to reach the signal

**Date:** 2026-10-12
**Type:** Live-data diagnosis (empirical, measured against `nt_signal_sim` + `nt_dynamic_tier_state`)
**Trigger:** Reachability is a red flag (tight buckets near-total SL; heavy reachability `nothing_survived` high) even though the ATR geometry has been widened (config bands) and harmonized (ATR pick as single source of truth, `dynamic_atr_light_mode: live`).
**Verdict:** The geometry is harmonized **in code and config**, but the ATR pick **never reaches the signal for 9/14 symbols** — those ship the tight **brick fallback**. The widening reached the config bands, not the book.

---

## 1. The measurement (live, last 24h, era-clean)

### 1a. ATR pick coverage — only 5/14 symbols have a pick

`nt_dynamic_tier_state` where `tier_type='dynamic_atr'`:

| symbol | tp_atr | sl_atr | source | n |
|---|---|---|---|---|
| AAPL | 8.00 | 5.00 | `pooled:equity` | 275 |
| US30USD | 8.00 | 5.00 | `pooled:equity` | 275 |
| BTCUSD | **13.00** | 2.00 | `pooled:crypto` | 260 |
| US500 | 2.93 | 0.81 | `tiered_atr` | 116 |
| XAGUSD | 6.39 | 2.69 | `tiered_atr` | 190 |

**No pick at all for: AUDUSD, EURUSD, GBPUSD, USDCAD, USDCHF, USDJPY (all 6 forex majors), XAUUSD, XAUAUD, XAUEUR (gold).**

### 1b. Shipped geometry = BRICK for 9/14 (verified by reconstruction)

Reconstructing `brick_size × tp_bricks / atr` and comparing to the shipped `tp_distance_atr`:

| symbol | n | brick tp/atr | shipped tp/atr | brick match | shipped sl/atr |
|---|---|---|---|---|---|
| AUDUSD | 71 | 1.50 | 1.50 | **100%** | 0.45 |
| EURUSD | 63 | 1.40 | 1.40 | **100%** | 0.53 |
| GBPUSD | 72 | 1.81 | 1.81 | **100%** | 0.90 |
| USDCAD | 55 | 1.80 | 1.80 | 93% | 0.90 |
| USDCHF | 59 | 0.77 | 0.77 | **100%** | 0.45 |
| USDJPY | 64 | 1.80 | 1.80 | **100%** | 0.45 |
| XAUUSD | 76 | 1.50 | 1.50 | **100%** | 0.45 |
| XAUAUD | 78 | 0.90 | 0.90 | **100%** | 0.44 |
| XAUEUR | 76 | 1.80 | 1.80 | **100%** | 0.88 |
| US500 | 21 | 2.99 | 2.93 | 5% | 0.69 |
| BTCUSD | 68 | 0.77 | 1.34 | 69% | 0.47 |
| US30USD | 7 | 7.78 | 8.00 | 57% | 3.50 |

Nine symbols ship **100% brick**. Only US500 ships its ATR pick cleanly; BTCUSD/US30USD are mixed (pick applied to a minority of signals — consistent with the pick being written recently, 19:20–21:05 today).

### 1c. The brick geometry lands in the dead bucket

Brick TP ≈ 1.5 ATR → the **1-2 bucket**. Era-clean reachability by bucket (BUCKET_INVERSION_HARMONIZATION.md, `nt_signal_sim`):

| bucket | n | TP% | SL% |
|---|---|---|---|
| <0.5 | 79 | 0.0% | 100% |
| 0.5–1 | 490 | 6.3% | 93.7% |
| **1–2** | **3,593** | **6.9%** | **93.1%** |
| 2–4 | 1,961 | 14.8% | 85.2% |
| 4+ | 1,882 | 13.0% | 61.5% |

XAUUSD 24h: 49/77 signals in the 1-2 bucket → 93.5% SL. XAUAUD 58/79 in 0.5-1 → 94.9% SL. **That is the red flag: the book trades a bracket whose own measured reachability is ~7%.**

---

## 2. Where it fails — three distinct points

### Failure 1 — the pick never reaches the signal for 9/14 symbols

`solve_free`/`select_tp_sl` require `min_signals` (80) resolvable 48-forward-bar paths per symbol. The book yields **2-3 paths/symbol** (only ~40 of 400 signals have 48 forward bars). So the per-symbol solve returns `None` → the fail-open chain goes to brick.

The asset-class **pooled fallback** (added today, POOLED_ATR_AND_SCORECARD_FILTER.md §1) rescues **equity** (AAPL, US30USD) and **crypto** (BTCUSD), and two symbols solve per-symbol (US500, XAGUSD). But **the pooled solve returns `None` for forex and for gold** — so those 9 symbols never get a pick. Gold's widened band (XAUUSD tp 4.60–5.30) is therefore **never exercised** — there is no gold pick to exercise it.

### Failure 2 — the fallback geometry is tight, and forex's clamp band is tight too

The brick fallback is the tight geometry (XAUUSD 1.5/0.45, XAUAUD 0.9/0.44 ATR). And for forex, even a *successful* pick would be tight: `dynamic_atr.by_class.forex` is `tp_atr 1.60–1.85, sl_atr 0.30–0.45` — so a pooled forex pick lands ~1.7/0.35 → the same 1-2 bucket. **The widening never touched forex.**

### Failure 3 — the pooled pick is clamped against the CLASS/global band, not the symbol's own band

`_pooled_pick` calls `dynamic_atr_pick(asset_class, cfg=get_dynamic_atr_config(asset_class, None), …)` — config resolved with `symbol=None`, so `by_symbol.<SYMBOL>` is skipped (precedence `by_symbol → by_class → global`). The pooled crypto solve therefore clamps against the **global** band (`tp_atr_max 20.0`), not BTCUSD's `by_symbol` band (`tp_atr_max 8.80`).

Result: BTCUSD's stored pick is **13.0 ATR** — a global grid value that **violates its own symbol band (max 8.80)** and is an RR≈6.5:1 artifact of exactly the class the GEOMETRY_HARMONY worklog flagged (the 12:1 case). The clamp gate that was supposed to reject it is applied against the wrong band. AAPL/US30USD 8.0/5.0 (pooled:equity, global band) may have the same issue vs their `by_symbol` bands (AAPL tp 6.0–6.8, US30USD tp 12.0–13.5).

---

## 3. What is NOT the failure

- The **harmonization** itself is correct: where a pick exists and ships (US500), the bucket, reachability, `p_win_bracket`, and EV all read the same geometry.
- The **reachability table** is not wrong — it correctly measures whatever geometry shipped, which is currently brick. Its tight-bucket readings are true readings of a tight bracket.
- The **EV-clobber** defect (ENFORCEMENT_CLOBBERS_REACHABILITY_EV.md) is separate and already fixed.

---

## 4. The fix surface (not built here — diagnosis only)

1. **Extend the pooled fallback to forex and metals** (or verify why their pooled solve returns `None` — likely the expectancy bar on the tight forex band, and a cold/metal path count). Without a pick for gold, the widened gold band is inert.
2. **Clamp the pooled pick against each inheriting symbol's own band** — pass the symbol's config (or re-validate the pooled pick against `by_symbol` clamps before `write_pick_to_store`). Otherwise pooled picks violate symbol bands (BTCUSD 13.0 vs 8.80).
3. **Widen the forex clamp band** (`by_class.forex`) — it is the binding constraint on the whole FX book and is tighter than the global default.
4. Only after a pick exists for a symbol does its **reachability re-measure on the ATR geometry** — the tight-bucket red flag should be re-read per symbol once it ships a widened pick.

---

## 5. Files / lineage

- `regime_platform/sweep_orchestrator/dynamic_tier_optimizer.py::_refresh_dynamic_atr` — the pooled fallback (config resolved with `symbol=None`).
- `regime_platform/sweep_orchestrator/tiered_atr.py` — `dynamic_atr_pick`, `write_pick_to_store`, clamp bands.
- `config/feature_flags.yaml` — `dynamic_atr.by_symbol` / `by_class.forex` bands.
- Worklogs: `20261012_GEOMETRY_HARMONY.md`, `20261012_POOLED_ATR_AND_SCORECARD_FILTER.md`, `20261012_BUCKET_INVERSION_HARMONIZATION.md`, `20261012_ENFORCEMENT_CLOBBERS_REACHABILITY_EV.md`.
- Evidence: `nt_dynamic_tier_state` (pick store), `nt_signal_sim` (shipped geometry), `nt_bracket_reachability` (measured buckets).
