# Worklog — Pulse 2026-09-27: due roadmap, decisions, and builds

**Date:** 2026-09-27  
**Repo:** `noble-trader-fastapi-backend`  
**Trigger:** “check roadmap and pulse check for due items and determine decisions” + “clarify #6 / write worklog / build”  
**Studio:** `64b7346` = local (publish-path fix live)

---

## 1. Plain language: what #6 actually is

Two separate quality problems. Neither is the cooldown. Both cut how many signals the system is willing to call “good.”

### 6a. Size is often zero even when the model likes the trade

The engine runs **two different scorers**:

| Scorer | What it measures | Example |
|---|---|---|
| **p_win / EV** (blend) | Model opinion: regime + pattern + TimesFM + Markov | “This looks like a 70% sell” |
| **Kelly size** (historical returns) | Math on **recent price history** (mean/variance of past moves) | “Recent path doesn’t support a bet” → **size = 0** |

They can disagree. A gold pair can print `p_win 0.75`, `EV +3.5` and still get `kelly = 0` because the last stretch of prices went the “wrong” way for that direction. Then the **min-Kelly quality check** rejects the signal.

**Last 24h:** **61% of directional scans had Kelly = 0.** That is why XAUAUD / XAGUSD / much of FX never publish.

**In one sentence:** *The quality gate is asking a price-history sizer for permission after the model already liked the trade — and the sizer often says “no bet.”*

### 6b. More than half of settled trades never hit TP or SL

They **expire** (time out) instead of resolving cleanly.

**7-day settled outcomes:** **56.8% expired** (5,107 / 8,989). Only ~43% hit TP or SL.

That means **trade shape** (take-profit / stop distance) is often wrong for how price actually moves. Even when we publish, many trades don’t convert into a clean win or loss — they fade. That hurts the scorecard and makes W/L look worse than the model’s opinions.

**In one sentence:** *We publish too few signals (6a), and of the ones that do trade, most die of old age instead of a clear exit (6b).*

### Why this is “quantity cut, quality not up”

- 6a **blocks** publishes (especially gold/FX).  
- 6b **pollutes** the book of what we did publish.  
- Neither is fixed by cooldown length. The publish-path redesign restored the queue; **#6 is the next quality track.**

**Not built in this worklog** (needs a separate product decision): switching the gate to binary Kelly from p_win×RR, or retuning TP/SL geometry. Documented as open work below.

---

## 2. Pulse snapshot (2026-09-27 05:03 UTC)

| Check | Result |
|---|---|
| Studio vs local | **Aligned** `64b7346` |
| Tracebacks | **0** |
| Errors | 1 × `record_signal_stream_meta_shadow` |
| Cooldown / stamps | `15.0min`, PUBLISHED / SUPPRESSED logs match D1–D5 |
| Resolved age | 3.7h |
| Expired (7d) | **56.8%** |
| Kelly = 0 (24h directional) | **61.4%** |
| Macro regime age | 2.6h |
| Heavy sweep | daily 16–29 rows |
| 24h publishes | EURUSD/GBPUSD/AAPL/BTCUSD only |

---

## 3. Decisions (this build)

| # | Item | Decision | Action |
|---|---|---|---|
| 1 | `calibration_resolve_hourly` | **Flip live** | `feature_flags.yaml` + roadmap |
| 2 | `heavy_sweep_daily_cadence` | **Resolve** | roadmap |
| 2b | `regime_state_persist` | **Resolve** (restore verified live) | roadmap |
| 2c | `kairos_daily_job` | **Bump review** (heartbeat healthy) | roadmap |
| 3 | `eod_resolver_scan_full` | **Bump** to after next full nightly; resolve if clean | roadmap |
| 4 | `macro_regime_mode` | **Keep shadow**; optional shorter-lookback later | roadmap note |
| 5 | Supabase 504 / RLS | **Index FK now**; RLS consolidation deferred with new date | SQL + roadmap |
| 6 | Kelly-zero + expired share | **Document only** — separate design (not built here) | §1 + open item |
| 7 | Publish-path 48h review | **Add roadmap row** due 2026-09-29 | roadmap |

---

## 4. Builds in this change

1. `calibration_resolve_hourly_mode: shadow` → **`live`**
2. SQL: index `nt_calibration_records (signal_id)` (504/RLS cheap win)
3. `nt_roadmap` updates per §3
4. New row `publish_path_redesign` (review 2026-09-29)

**Not built:** Kelly gate redesign, geometry/expiry retune, macro live flip, full RLS cleanup.

---

## 5. Follow-ups (explicitly open)

| ID | Open question | Gate |
|---|---|---|
| Q1 | **DECIDED 2026-09-27:** drop historical Kelly as a gate; **variable binary Kelly (shadow)** with Kairos event-time tiers (never age). See `20260927_CHANGELOG_EV_ENGINE_KELLY_GATE.md`. | Built shadow; promote later |
| Q2 | Geometry / ATR TP-SL to cut the 57% expiry rate | Kairos geometry track (10-05 cluster) |
| Q3 | Macro shorter lookback experiment | After hourly-resolve live settles |
| Q4 | RLS policy consolidation (119 stacked policies) | Separate cleanup |

---

## 6. Verification after Studio pull of this change

- Log: `[calib:hourly]` still `mode: live` (not shadow).  
- Resolved TP/SL land within ~1.5h of exit (not 11h).  
- 504 frequency vs prior pulse (report, don’t assume).  
- Publish-path 48h counts on 09-29 roadmap row.
