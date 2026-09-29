# Control-symbol profitability + **class** geometry (worklog)

**Date:** 2026-09-29 · **Type:** analysis + build · **Parent:** `reports/20260929_atr_decision_read.md`  
**Architecture (binding):** `dynamic_atr_pick(asset_class)` is **already per-class**. There are **no per-symbol geometry configs**. One mechanism, class-band grids only.

**Corrected framing (2026-09-29):** the 4-symbol “control” replay was a **probe**, not a product split. Production path:

```text
nt_symbol.asset_class → asset_class_key() → dynamic_atr_pick(class) → TP/SL ATR
                                      ↑
                         get_dynamic_atr_config(class)  # by_class overlays
```

Goal: class bands that make **every** symbol in a class go through the same math — including XAUUSD, XAUEUR, USDCAD, AAPL.

---

## 0. Finding — expiry is **not** generic (settled 09-20→27)

| Symbol | P/C | n | exp% | wr | pnl_b | avg TP ATR | avg SL ATR |
|---|---|---:|---:|---:|---:|---:|---:|
| EURUSD | P | 669 | **86%** | 0.17 | −0.46 | **37.8** | **23.1** |
| AUDUSD | P | 670 | **85%** | 0.15 | −0.44 | 54.4 | 31.8 |
| USDCAD | **C** | 669 | **84%** | 0.10 | −0.87 | 53.5 | 31.0 |
| USDCHF | P | 669 | 75% | 0.15 | −0.85 | 45.1 | 26.6 |
| USDJPY | P | 668 | 72% | 0.04 | −0.13 | 29.4 | 14.8 |
| XAUAUD | P | 671 | 67% | 0.23 | −0.48 | 16.0 | 8.0 |
| GBPUSD | P | 672 | 60% | 0.16 | −0.15 | 26.7 | 14.9 |
| BTCUSD | P | 671 | 57% | 0.23 | **+0.68** | 24.0 | 13.9 |
| XAUEUR | **C** | 671 | 50% | 0.10 | −0.88 | 15.0 | 6.8 |
| US30USD | P | 666 | 49% | 0.42 | **+0.81** | 23.0 | 12.7 |
| US500 | P | 668 | 36% | 0.27 | −0.38 | 23.8 | 13.1 |
| XAUUSD | **C** | 671 | 36% | 0.19 | −0.60 | 11.5 | 5.9 |
| XAGUSD | P | 671 | **9%** | 0.29 | −0.37 | 9.1 | 4.5 |
| AAPL | **C** | 671 | **3%** | 0.18 | **−2.59** | 11.4 | 5.6 |

**Trend:** expiry tracks **bracket width** (TP/SL ATR), not the symbol name.  
Wide FX (TP 30–55 ATR) → 70–86% expired. Tight metals/AAPL (TP ~9–12) → 3–36%.

### Basket geometry is **not sound**

One ATR multiple for all 14 symbols produces: same “geometry,” different reachability.  
**USD is the outlier class** (EURUSD/AUDUSD/USDCAD/USDCHF/USDJPY): need **much tighter** absolute ATR bands or per-vol-tier grids.

---

## 1. AAPL — replay or backfill?

| | Purpose | Needed for AAPL? |
|---|---|---|
| **Backfill** `pct_expired` on widened bucket | Unblock `should_promote_atr_candidates` (currently fail-closed on missing lag) | **Cheap yes** — but AAPL already has **3% expiry**; not the bottleneck |
| **Geometry replay** | Find a combo that is actually **+EV** | **Yes — this is the goal** |

**AAPL answer: replay (profitability), then backfill (gate hygiene).**  
AAPL is not expiring — it is **losing to SL** (`pnl −2.59` with wr 18%). Waiting on promote data will not fix that. Run a symbol-specific replay (tighter SL / smaller TP) and **backfill** only so the promote rule stops ignoring AAPL.

---

## 2. Control path to profitability

| Symbol | Problem | Fix |
|---|---|---|
| **USDCAD** | Same as EURUSD — TP 53 / SL 31 → 84% exp | **Force tighter SL band (≤2 ATR)** / share FX grid with EURUSD experiment |
| **XAUEUR** | exp 50%, wr 10% | Gold grid: SL 0.5–2, TP mid (9–17); pair with XAUUSD |
| **XAUUSD** | exp 36%, wr 19%, already tight | **Force SL 0.5–2** (OOS +0.68); lift WR |
| **AAPL** | exp OK, **SL losses** | **Replay AAPL geometry** (likely smaller TP / tighter SL); backfill reachability |

### Build this session

1. `scripts/control_geometry_replay.py` — per-symbol offline geometry search on decided signals (TP/SL ATR grid), reports best band per control symbol.  
2. `tiered_atr.should_promote_atr_candidates` — when `base == wide`, allow promote if **SL can tighten** (fixes XAUUSD/USDCAD stuck on `4+`).  
3. Worklog + roadmap note.

---

## 3. Sound geometry policy (recommendation)

**Stop one-size basket.** Use **class bands**:

| Class | Symbols | SL ATR | TP ATR |
|---|---|---|---|
| Major FX | EURUSD, GBPUSD, USDJPY, USDCHF, USDCAD, AUDUSD | **0.5–2** | **3–8** (not 30–50) |
| Gold / silver | XAUUSD, XAUEUR, XAUAUD, XAGUSD | **0.5–2** | **6–12** |
| Indices / BTC | US500, US30USD, BTCUSD | 0.5–3 | 8–15 |
| Single names | AAPL, … | **0.5–1.5** | **4–8** (replay to lock) |

OOS already shows **SL 0.5–2** is the only band with positive expectancy.

---

## 4. Replay results (settled 09-20→27) — per-symbol best

| Symbol | Best cell | n | exp% | wr | **pnl_bricks** |
|---|---|---:|---:|---:|---:|
| **AAPL** | **TP 9–17 / SL 2–4** | 164 | 0% | **0.34** | **+0.73** |
| **XAUUSD** | TP 9–17 / SL 8–40 (or 12–24/8–40 +0.92) | 28 / 124 | 68% / 69% | 0.67 / 0.24 | **+2.34 / +0.92** |
| **XAUEUR** | TP 6–12 / SL 4–8 | 195 | 58% | 0.16 | **+0.13** (thin) |
| **USDCAD** | **none +EV** | — | — | — | all negative (−0.78 best) |

**Surprise:** **SL 0.5–2 is not universal.** AAPL wants **SL 2–4**; XAUUSD profits on **wide SL 8–40** (many expirations but winners pay). Basket “one SL band” is wrong.

### Control profit policy (updated)

| Symbol | Target geometry | Action |
|---|---|---|
| **AAPL** | **TP 9–17 / SL 2–4** | **Replay → force this band** (was −2.59 live avg; +0.73 in-band) |
| **XAUUSD** | TP 9–24 / SL 8–40 | Allow wide SL; stop forcing 0.5–2 |
| **XAUEUR** | TP 6–12 / SL 4–8 | Soft target; thin edge |
| **USDCAD** | **Do not trade wide FX** until a +EV cell exists | Gate / skip, or re-replay after more settles |

### Build shipped

- `scripts/control_geometry_replay.py` + report `20260929_control_geometry_replay.md`  
- `should_promote_atr_candidates`: same-bucket (`4+` stuck) now admits ATR pool (SL tightening)

---

## 5. Decision summary

1. **Expiry tracks bracket width** — one global grid is wrong; **class overlays** are the lever (not symbol overrides).  
2. **AAPL** is in `equity` with US30/US500 — class pick **TP 12–24 / SL 4–8** (wr 0.46, +0.91). No AAPL-only path.  
3. **XAUUSD** is `commodity` with gold/silver — class pick **TP 3–8 / SL 2–4** (wr 0.41, +0.65).  
4. **Forex / crypto** — no sound +EV cell under filters (wr≥0.2, exp≤0.5); keep global fallback until more settles.  
5. Hybrid promote stays; `dynamic_atr` full flip **2026-10-03**.

---

## 6. Build (this session)

| Piece | What |
|---|---|
| `feature_flags.dynamic_atr.by_class` | `commodity` + `equity` overlays from class replay |
| `get_dynamic_atr_config(asset_class)` | Merge overlay by `asset_class_key` |
| `heavy_sweep` | Pass `sym.asset_class` into config |
| `scripts/dynamic_atr_class_replay.py` | Class-level grid (all symbols) — source of truth for overlays |
| Report | `reports/20260929_dynamic_atr_class_replay.md` |

**Not done:** per-symbol tables. **Not done:** forex/crypto overlays (no sound cell).
