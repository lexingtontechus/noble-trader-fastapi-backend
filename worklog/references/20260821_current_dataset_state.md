# 2026-08-21 — Current Dataset State: What Happens With L1/L2/L3 Fixes

## Current State of ALL 14 Symbols (Latest Sweep)

Verified via Supabase REST API on 2026-08-21.

| Symbol | qualified | p_win | kelly | RR | EV | markov_up | size_mult | Gates Failed |
|--------|-----------|-------|-------|-----|-----|-----------|-----------|-------------|
| AUDUSD | ✗ | 0.0000 | 0.0000 | 1.6 | -0.0089 | 0.000 | 1.0 | p_win, kelly, RR, EV |
| BTCUSD | ✗ | 0.6118 | 0.3338 | 1.7 | 344.62 | 0.004 | 1.0 | RR |
| EURUSD | ✗ | 0.9624 | 0.4000 | 2.7 | 0.0165 | 1.000 | **0.6** | **ALL PASS** |
| GBPUSD | ✗ | 0.0000 | 0.0000 | 1.7 | -0.0178 | 0.000 | 1.0 | p_win, kelly, RR, EV |
| US30USD | ✗ | 0.0000 | 0.0000 | 2.0 | -671.65 | 0.000 | 1.0 | p_win, kelly, EV |
| US500 | ✗ | 0.8780 | 0.0200 | 1.6 | 206.35 | 0.000 | **0.6** | RR |
| USDCAD | ✗ | 0.0000 | 0.0000 | 1.7 | -0.0078 | 0.000 | 1.0 | p_win, kelly, RR, EV |
| USDCHF | ✗ | 0.1064 | 0.0000 | 1.7 | -0.0140 | 0.000 | 1.0 | p_win, kelly, RR, EV |
| USDJPY | ✗ | 0.0379 | 0.0000 | 1.7 | -2.7825 | 0.000 | 1.0 | p_win, kelly, RR, EV |
| XAGUSD | ✗ | 0.0145 | 0.0000 | 1.6 | -0.8627 | 0.018 | 1.0 | p_win, kelly, RR, EV |
| XAUAUD | ✗ | 0.0000 | 0.0000 | 1.7 | -332.73 | 0.000 | 1.0 | p_win, kelly, RR, EV |
| XAUEUR | ✗ | 0.7892 | 0.0800 | 3.3 | 117.13 | 0.929 | 1.0 | **ALL PASS** |
| XAUUSD | ✗ | 0.0382 | 0.0000 | 1.7 | -119.22 | 0.000 | 1.0 | p_win, kelly, RR, EV |
| AAPL | ✗ | 0.7007 | 0.0190 | 2.7 | 3.7351 | 0.017 | 1.0 | kelly<0.02 |

**0/14 symbols are qualified.** Root causes by category:

### Category 1: HMM Degeneracy (p_win=0, markov_up=0)
**7 symbols**: AUDUSD, GBPUSD, US30USD, USDCAD, USDJPY, XAUAUD, XAUUSD
- All have markov_up=0.000 → HMM state collapse → logit(0)=-34.5 → p_win≈0
- **L1+L2 fix addresses these**: p_win should rise to ~0.585 (L1) → ~0.811 (L1+L2)

### Category 2: RR Gate (RR < 2.0)
**4 symbols**: BTCUSD (1.7), US500 (1.6), USDCHF (1.7), XAGUSD (1.6)
- tp_bricks=5, sl_bricks=3 → RR=5/3=1.67 < 2.0
- **NOT fixed by L1/L2/L3** — this is a brick parameter issue
- Fix: increase tp_bricks to 6+ in Redis config, or lower MIN_RR to 1.5

### Category 3: Kelly Gate (eff_kelly < 0.02)
**1 symbol**: AAPL (0.019) — borderline, would pass with any p_win increase

### Category 4: Content Deduplication
**2 symbols**: EURUSD (passes ALL gates but qualified=False), XAUEUR (passes ALL gates but qualified=False)
- content_hash == self._last_signal_hash.get(dedup_key) → suppressed at orchestrator.py:2286
- In-memory dict → resets on process restart

### Category 5: Combined Issues
AUDUSD, GBPUSD (HMM degeneracy + RR gate), US30USD (HMM degeneracy + negative EV)

---

## What L1 Raising "An Alarm" (Actually) Does

L1 (the _LOGIT_CLAMP change) does NOT raise an alarm or trigger corrective action. It is a **passive numerical safeguard** in ev_engine.py:

### Before (clamp=1e-15)
- Degenerate sources (markov=0.0, timesfm=0.0) → logit(1e-15) = -34.5
- This value dominated the 4-source blend (weight 0.20) → p_win ≈ 0.0
- EOD correction saw p_win < 0.5 → never computed calibration bias for degenerate symbols
- EOD correction skipped them (if bias is None: continue → deadlock)

### After (clamp=0.01)
- Degenerate sources → logit(0.01) = -4.6 instead of -34.5
- Other sources (p_regime=0.997 → logit=5.0, p_timesfm=0.50 → logit=0.0) can countervail
- p_win rises from ~0.0004 → ~0.585 (L1 only) → ~0.811 (L1+L2)

**L1 does NOT trigger any corrective action by itself.** It is the **enabler** that makes the EOD correction pathway computable, which then unblocks L3's degeneracy_breaker → emergency_heavy_sweep → HMM refit chain.

## What Happens With the Current Dataset After L1+L2+L3 Deployment

### Degenerate Symbols (7: AUDUSD, GBPUSD, US30USD, USDCAD, USDJPY, XAUAUD, XAUUSD)
```
Step 1 — L1 kicks in: logit(0.0) = logit(0.01) = -4.6 (was -34.5)
Step 2 — p_win rises: 0.0004 → 0.585 (still < 0.55 threshold)
Step 3 — L2 kicks in: uniform T3 → markov_up = 1/3 (was 0.0)
Step 4 — p_win rises further: 0.585 → 0.811 (NOW > 0.55 threshold)
Step 5 — Signal QUALIFIES → paper position opened → tp_hit/sl_hit tracked
Step 6 — EOD resolution creates calibration records (first in 7+ days)
Step 7 — L3 kicks in: bias computable → bayesian_penalty/degeneracy_breaker → emergency heavy sweep
Step 8 — Heavy sweep refits HMM on fresh data → may escape collapse naturally
Step 9 — Normal EOD correction cycle engages → feedback loop restored
```

### Content-Dedup Symbols (2: EURUSD, XAUEUR)
- Both pass ALL gates but are suppressed by content deduplication (orchestrator.py:2286)
- In-memory _last_signal_hash → resets on process restart
- **After process restart**: EURUSD's signal passes through immediately (all gates pass)
- **L2 benefit**: uniform T3 prevents markov_up from being stuck at 1.0 (absorbing state)
- **L1 benefit**: clamp prevents p_win from collapsing if markov dips slightly

### RR-Gate Symbols (4: BTCUSD, US500, USDCHF, XAGUSD)
- L1/L2/L3 do NOT fix the RR gate (1.67 < 2.0)
- Requires parameter change: tp_bricks→6 in Redis config or MIN_RR=1.5
