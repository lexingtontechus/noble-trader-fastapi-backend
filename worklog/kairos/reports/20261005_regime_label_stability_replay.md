# Regime-label stability replay — live result

**Date:** 2026-10-05 · **Script:** `scripts/replay_regime_label_stability.py`
**Run:** Studio (`noble-trader-unified`), live TDVA, 200 ticks × 200-bar window × 5 symbols
**JSON:** `20261005_regime_label_stability_replay.json`

## Why

The 500-tick / 5-symbol flip-rate sweep behind the debouncer decision
(`worklog/20260901_REGIME_LABEL_STABILITY_SCOPE.md`) was ad hoc — never kept as a
runnable artifact. This is that artifact, plus two read levels the original didn't
measure.

`RegimeHMM.fit()` rebuilds a fresh `GaussianHMM` every tick and re-inits from k-means;
the rolling window shifts one bar per tick, occasionally enough to move the init across
a cluster boundary → a different EM optimum → a different `regime_label`. That label
**picks trade direction** and drives `detect_regime_shift`, so its stability is a live
decision input.

## Method

Cold-fit the rolling window tick-by-tick (production baseline — warm-start is shelved).
Measure tick-to-tick flip rate at three levels:

- **full** — the raw `regime_label` (`low_vol_bull` → `low_vol_strong_bull` …)
- **cell** — `vol_tier|direction` via `kairos.regime_cell` (strength collapsed) — the
  coarser read the Kairos pipeline keys on
- **trend** — `trend_filter.compute_trend_regime` on the same window
  (`trending_*` / `reversal_*` / `consolidating`) — the platform's **momentum / trend**
  read. Chop and consolidation are not new regime labels; they are this layer
  recognising that momentum has faded (no fresh crossover + neutral RSI →
  `consolidating`).

`RegimeLabelDebouncer` applied at n_confirm = 2/3/4 in parallel on the full label.

## Result (live, 2026-10-05)

| Symbol | ticks | raw full | cell | trend | n2 | **n3** | n4 | consol | conf |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| US500 | 200 | 69.3% | 54.8% | 10.1% | 14.6% | **5.5%** | 3.5% | 76.0% | 0.686 |
| XAUAUD | 200 | 71.9% | 65.8% | 10.1% | 14.6% | **6.5%** | 3.0% | 75.5% | 0.649 |
| GBPUSD | 200 | 80.9% | 72.9% | 15.1% | 9.0% | **4.0%** | 1.5% | 65.5% | 0.691 |
| AAPL | 200 | 55.8% | 40.7% | 13.6% | 11.1% | **5.0%** | 3.5% | 73.0% | 0.793 |
| XAGUSD | 200 | 78.4% | 73.9% | 15.1% | 13.1% | **3.0%** | 0.5% | 55.5% | 0.657 |

**Means:** raw full **71.3%**, cell **61.6%**, trend **12.8%**, debounced n=3 **4.8%**,
consolidating **69.1%**.

Major/minor split (full label): US500 51/87 · XAUAUD 49/94 · GBPUSD 58/103 ·
AAPL 51/60 · XAGUSD 73/83.

## Findings

1. **The raw full label churns on 71% of ticks** — worse than the worklog's 36-58%,
   because this window is 1h (the light sweep re-fetches the same hourly bar until it
   rolls, so tick-to-tick here is really hour-to-hour) and the market has been choppier.
   **The cell read cuts that to 62%** (strength collapse removes ~9pp), confirming most
   churn is strength buckets, not direction — but direction still churns a lot.
2. **The debouncer holds unconditionally: 71.3% → 4.8% at n=3**, every symbol, no
   exception. Reproduces the worklog's conclusion on fresh data.
3. **The trend/momentum read is far more stable (12.8%) than the HMM label (71.3%)** —
   ~5.5× steadier. That is the point: the platform's momentum layer (`trend_filter`)
   already gives a stable trend/direction read; the instability is confined to the
   HMM's 4×4 bucket ranking, not to "is there a trend / which way".
4. **`consolidating` is the dominant trend read (69.1%)** — consistent with the live
   14-day distribution (60.2% `consolidating`). The platform recognises faded momentum;
   it is not mislabelling it as a trend.
5. **AAPL again the outlier**: lowest raw flip (55.8%), highest confidence (0.793),
   lowest cell flip (40.7%) — the symbol was in a genuine, low-vol consolidation. Same
   shape the worklog's warm-start comparison tripped on.

## Reproduce

```bash
# On the Studio (live TDVA creds):
cd /teamspace/studios/this_studio/noble-trader
set -a && . ./.env.local && set +a
python -m scripts.replay_regime_label_stability \
  --ticks 200 --window 200 --symbols US500,XAUAUD,GBPUSD,AAPL,XAGUSD \
  --json worklog/kairos/reports/20260905_regime_label_stability_replay.json
```

Local: same command with the project venv; needs `.env.local` TDVA key. The pure core
(`sweep_symbol`) is unit-tested in `tests/test_replay_regime_label_stability.py` (15
tests, no network).
