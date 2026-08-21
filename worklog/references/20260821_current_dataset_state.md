# 2026-08-21 — Current Dataset State: What Happens With L1/L2/L3 Fixes

## Current State of Affected Symbols

Verified via Supabase REST API on 2026-08-21.

### Degenerate Symbols (p_win=0.0, HMM collapse)

| Symbol | p_win | markov_up | EV | Kelly | RR | Gates Failed |
|--------|-------|-----------|-----|-------|-----|-------------|
| AUDUSD | 0.0000 | 0.000 | -0.0089 | 0.0000 | 1.60 | p_win, kelly, RR, EV |
| GBPUSD | 0.0000 | 0.000 | -0.0178 | 0.0000 | 1.71 | p_win, kelly, RR, EV |
| US30USD | 0.0000 | 0.000 | -671.65 | 0.0000 | 2.00 | p_win, kelly, EV |

**Root cause**: HMM state collapse (all 5 states → "FLAT", T3 absorbing, p_markov=0)
**Fix applied**: L1 (clamp=0.01) + L2 (uniform T3 fallback)
**Expected result**: p_win rises from 0.0004 → 0.585 (L1) → 0.811 (L1+L2)

### "Recovered" Symbol: EURUSD

| Field | Value | Gate | Result |
|-------|-------|------|--------|
| p_win=0.9624 | MIN_P_WIN=0.55 | ✓ PASS |
| eff_kelly=0.40 | MIN_KELLY=0.02 | ✓ PASS |
| RR=2.67 | MIN_RR=2.0 | ✓ PASS |
| EV=0.0165 | MIN_EV=0.0 | ✓ PASS |
| regime_conf=1.0 | MIN_REGIME_CONF=0.60 | ✓ PASS |
| timesfm_conf=0.0001 | (soft mode) | ✓ PASS |

**Yet qualified=False** — blocked by **content deduplication** (orchestrator.py:2286):
- Signal content (entry price, SL, TP, brick params) hasn't changed since last sweep
- `content_hash == self._last_signal_hash.get(dedup_key)` → suppressed
- This is an **in-memory** dict (`_last_signal_hash`), resets on process restart

### Near-Miss: BTCUSD

| Field | Value | Gate | Result |
|-------|-------|------|--------|
| p_win=0.6118 | 0.55 | ✓ PASS |
| kelly=0.3338 | 0.02 | ✓ PASS |
| EV=344.62 | >0 | ✓ PASS |
| RR=1.67 | 2.0 | ✗ FAIL |

**Blocked by RR gate only** — `tp_bricks=5, sl_bricks=3` → RR=5/3=1.67 < 2.0
- This is a separate issue, documented in AGENTS.md lines 100-104
- Fix: change brick params (tp_bricks→6 in Redis config) or lower MIN_RR to 1.5

---

## What L1 Raising "An Alarm" (Actually) Does

L1 (the `_LOGIT_CLAMP` change) does NOT raise an alarm. It is a **passive numerical safeguard** in `ev_engine.py`:

### Before (clamp=1e-15)
- Degenerate sources (markov=0.0, timesfm=0.0) → `logit(1e-15) = -34.5`
- This value dominated the 4-source blend (weight 0.20) → p_win ≈ 0.0
- EOD correction saw p_win < 0.5 → never computed calibration bias for degenerate symbols
- EOD correction skipped them (`if bias is None: continue` → deadlock)

### After (clamp=0.01)
- Degenerate sources → `logit(0.01) = -4.6` instead of `-34.5`
- Other sources (p_regime=0.997 → `logit=5.0`, p_timesfm=0.50 → `logit=0.0`) can countervail
- p_win rises from ~0.0004 → ~0.585 (L1 only)
- EOD correction computes bias → emergency heavy sweep triggered (L3 pathway unblocked)
- Heavy sweep refits HMM → if T3 still collapses → L2 uniform T3 kicks in → p_markov=1/3
- p_win rises further to ~0.81 → signal qualifies → calibration records accumulate

L1 does NOT trigger any corrective action by itself. It is the **enabler** that unblocks L3's emergency heavy sweep pathway.

## What Happens With the Current Dataset After Deployment

### AUDUSD, GBPUSD, US30USD (currently p_win=0.0):
- L1: `__logit_clamp = 0.01` → `logit(0.0) = logit(0.01) = -4.6` instead of `-34.5`
- p_win rises from ~0.0004 → ~0.585
- EOD correction now computes bias (was None → now computable)
- If |bias| > 0.15 → `bayesian_penalty` written → drift_monitor catches on next sweep
- If |bias| > 0.15 AND p_win < 0.50 → `emergency_heavy_sweep` + `degeneracy_breaker` triggered (L3)
- Heavy sweep refits HMM → if T3 still collapses → L2 uniform T3 → p_markov=1/3
- p_win rises further to ~0.81 → signal qualifies → calibration records accumulate
- Once calibration records exist, normal EOD correction cycle engages → feedback loop restored

### EURUSD (currently qualified=False due to dedup):
- Content dedup is in-memory (`_last_signal_hash`) → resets on process restart
- Once process restarts, EURUSD's identical signal passes through (all gates pass)
- With L1, even if signal content changes slightly, p_win won't collapse to 0
- L2's uniform T3 ensures markov_up won't be stuck at 1.0 (absorbing state)

### BTCUSD (currently qualified=False due to RR):
- p_win=0.6118 > 0.55, passes ALL gates except RR (1.67 < 2.0)
- L1/L2/L3 do NOT fix the RR gate — that's a parameter tuning issue
- Requires: changing brick params (tp_bricks→6 in Redis config) or lowering MIN_RR to 1.5
- This is a pre-existing issue documented in AGENTS.md
