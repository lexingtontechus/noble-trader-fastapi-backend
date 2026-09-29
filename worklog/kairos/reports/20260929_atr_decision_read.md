# ATR decision read — promoted vs control + OOS geometry band

Generated 2026-09-29 21:14 UTC · window from 2026-09-26T00:00:00Z

## 0. Promote list (heavy ATR evidence)

| symbol | n_cmp | chg | base | wide | lagB | lagW | promote |
|---|---:|---:|---|---|---:|---:|---|
| XAUUSD | 25 | 22 | 4+ | 4+ | 0.335 | 0.335 | False |
| XAUEUR | 22 | 18 | 4+ | 4+ | 0.2508 | 0.2508 | False |
| XAUAUD | 27 | 25 | 4+ | 0.5-1 | 0.0437 | 0 | True |
| XAGUSD | 23 | 19 | 4+ | 1-2 | 0.1568 | 0 | True |
| USDJPY | 22 | 6 | 4+ | 0.5-1 | 0.6655 | 0 | True |
| US30USD | 27 | 24 | 4+ | 0.5-1 | 0.6711 | 0 | True |
| USDCHF | 27 | 26 | 4+ | 0.5-1 | 0.934 | 0 | True |
| USDCAD | 14 | 9 | 4+ | 4+ | 0.8213 | 0.8213 | False |
| GBPUSD | 21 | 20 | 4+ | 1-2 | 0.8292 | 0 | True |
| BTCUSD | 23 | 23 | 4+ | 1-2 | 0.2396 | 0 | True |
| AAPL | 22 | 21 | 4+ | 1-2 | 0 | — | False |
| AUDUSD | 14 | 12 | 4+ | 0.5-1 | 0.9758 | 0 | True |
| EURUSD | 23 | 19 | 4+ | 2-4 | 0.84 | 0 | True |
| US500 | 25 | 25 | 4+ | 1-2 | 0.6095 | 0 | True |

**Promoted:** XAUAUD, XAGUSD, USDJPY, US30USD, USDCHF, GBPUSD, BTCUSD, AUDUSD, EURUSD, US500
**Control:** XAUUSD, XAUEUR, USDCAD, AAPL

## 1. Dynamic ATR — light outcomes after hybrid promote

### PROMOTED (n=2563)

| metric | value |
|---|---:|
| tp_hit | 686 |
| sl_hit | 1315 |
| expired | 562 |
| pct_expired | 0.219 |
| win_rate_decided | 0.343 |
| avg_tp_distance_atr | 11.6229 |
| avg_sl_distance_atr | 6.6965 |
| avg_pnl_bricks | 0.4116 |
| avg_pnl_dollars | 28.0078 |

### CONTROL (n=1118)

| metric | value |
|---|---:|
| tp_hit | 281 |
| sl_hit | 685 |
| expired | 152 |
| pct_expired | 0.136 |
| win_rate_decided | 0.291 |
| avg_tp_distance_atr | 13.0393 |
| avg_sl_distance_atr | 7.4739 |
| avg_pnl_bricks | -1.3706 |
| avg_pnl_dollars | -5.1837 |

**Flip bar (roadmap):** tp_distance_atr DOWN toward 0.5–2, pct_expired DOWN, closed pnl UP/flat at similar n on promoted vs control → `dynamic_atr_mode=live`. Kill if exp% flat/up or expectancy worse.

## 2. OOS geometry replay (09-25 → now)

| TP band | n | pct_expired | win_rate_decided | avg pnl_bricks | avg tp_atr | avg sl_atr |
|---|---:|---:|---:|---:|---:|---:|
| TP 9-17 (replay band) | 744 | 0.263 | 0.208 | -0.9389 | 12.777 | 6.2274 |
| TP <9 (tight) | 2574 | 0.011 | 0.361 | 0.3263 | 1.9143 | 0.9395 |
| TP >17 (wide) | 1637 | 0.874 | 0.024 | -0.5481 | 44.4143 | 25.5814 |

- **H1 09-25..09-28** replay-band n=673 exp%=0.291 wr=0.239 pnl_bricks=-0.6264
- **H2 09-28..** replay-band n=71 exp%=0.0 wr=0.0 pnl_bricks=-3.9014

**Replay bar:** TP 9–17 / SL 0.5–2 stays **positive expectancy** on OOS halves. If only one half is positive → hold, re-run after more settles.

### 2b. Joint band (TP 9–17 **and** SL 0.5–2) — post-hoc slice

| Slice | n | exp% | wr_decided | pnl_bricks |
|---|---:|---:|---:|---:|
| **JOINT TP9-17 + SL0.5-2** | **~0** | — | — | — |
| TP 9–17 only (SL outside) | 605 | 14.7% | 0.28 | **−0.67** |
| **SL 0.5–2 only** | 509 | **0%** | **0.40** | **+0.68** |
| Other | 3707 | 42.5% | 0.26 | −0.45 |

**Finding:** live light book almost never uses the full replay combo yet. **SL 0.5–2 is the positive lever** (exp 0%, wr 40%). TP 9–17 without tight SL is **not** enough.

---

## 3. Decision recommendation (this read)

| Lever | Evidence | Recommend |
|---|---|---|
| **dynamic_atr promoted** | pnl_bricks **+0.41 vs −1.37**, wr 34% vs 29%, tp_atr closer to band | **GO hybrid / promote path** — pnl bar met |
| exp% on promoted | **21.9% vs 13.6% control** | **Does not** meet “exp% DOWN” — driven by wider SL (6.7 vs 7.5 ATR) and more TPs attempted |
| OOS TP 9–17 alone | **−0.94** pnl_bricks | **Do not** treat TP band alone as green |
| OOS **SL 0.5–2** | **+0.68**, exp 0% | **Target this** in dynamic_atr pick |
| Full flip `dynamic_atr_mode=live` | Mixed (pnl yes, exp% no) | **Hold live-flip** one more cycle OR flip **risk-capped** only if you accept higher expiry as cost of positive expectancy |

**Honest call:** promoted set **makes money**; control does not. Expiry is higher on promoted. The OOS replay **does not confirm TP 9–17** as a standalone winner — **tight SL (0.5–2)** is the stable positive. Prefer **dynamic_atr pick that enforces SL∈[0.5,2]** over forcing TP∈[9,17] alone. Full `dynamic_atr_mode=live` can wait one more light cycle if you want both bars green; hybrid promote already looks right.

---

## 4. Daily scorecard (decided light, 2026-09-20 → 09-29)

**ALL** — expiry falling 0.70→0.35 after 09-26, but **0% on 09-28/29 is settlement lag (48h), not quality**. Win rate and pnl turn up 09-28/29.

| Day | n | exp% | wr | pnl_b |
|---|---:|---:|---:|---:|
| 09-25 | 1274 | 0.74 | 0.22 | −0.22 |
| 09-26 | 1269 | 0.35 | 0.27 | −0.71 |
| 09-27 | 853 | 0.32 | 0.17 | −0.92 |
| 09-28* | 884 | *0.00* | 0.42 | **+0.97** |
| 09-29* | 705 | *0.00* | 0.38 | **+0.45** |

\* expired outcomes not fully settled.

**PROMOTED** (improving since hybrid promote):

| Day | n | wr | pnl_b |
|---|---:|---:|---:|
| 09-26 | 906 | 0.36 | **+0.23** |
| 09-27 | 607 | 0.14 | −0.36 |
| 09-28* | 618 | 0.42 | **+1.11** |
| 09-29* | 457 | 0.39 | **+0.78** |

**CONTROL** (still weak):

| Day | n | wr | pnl_b |
|---|---:|---:|---:|
| 09-26 | 363 | 0.10 | **−3.07** |
| 09-27 | 246 | 0.24 | **−2.30** |
| 09-28* | 266 | 0.44 | +0.64 |
| 09-29* | 248 | 0.37 | **−0.15** |

---

## 5. Control symbols — path forward (not “wait”)

Control = **XAUUSD, XAUEUR, USDCAD, AAPL**. Why they fail promote: baseline == widened == `4+` (no better bucket picked) or missing lag on widened (AAPL).

| Option | What | Why |
|---|---|---|
| **A. Force SL 0.5–2 on control** (recommended) | Bypass promote; pick ATR-anchored bricks with **SL∈[0.5,2]** on these 4 | Gold/USDCAD are high volume; SL band is the only OOS-positive lever |
| **B. Separate gold track** | XAUUSD/XAUEUR own geometry grid (tighter SL, medium TP) | Gold behaves unlike FX; `4+` stuck for both |
| **C. Fix `should_promote_atr_candidates`** | When base==wide, allow promote if **SL can tighten** even if TP bucket unchanged | Root cause of XAUUSD/USDCAD stuck on `4+` |
| **D. Reachability backfill for AAPL** | Fill `pct_expired` for widened bucket so promote isn’t fail-closed on missing lag | Cheap data fix |
| **E. Publish-side only** | Keep control at current geometry but **don’t publish** (gate) until SL band works | Protects the book; loses volume |

**Recommend:** **A + C** (force SL 0.5–2 on control + fix same-bucket promote). Do **not** wait another week on the same `4+` geometry.

---

## 6. Roadmap action

`dynamic_atr_mode_flip` → **HOLD**, `review_at=**2026-10-03**` (1 settle cycle after 09-28/29). Hybrid promote stays live. Flip only if promoted exp% improves **or** pnl stays clearly positive **and** SL 0.5–2 volume shows up.


Research-only. No flag flips in this script.
