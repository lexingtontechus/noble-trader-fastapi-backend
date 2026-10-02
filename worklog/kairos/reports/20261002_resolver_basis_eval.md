# §3.1 kill criterion — does the intrabar basis change the label?

Generated 2026-10-02 04:41 UTC · lookback 60d · horizons 4h, 24h, 48h · light sweep

Both labels below are computed on the **same intrabar basis** (`mae_*`/`mfe_*` are intrabar highs/lows), so the difference between them is the ghost edge *without* the close-vs-intrabar confound that made the earlier 57.7% figure an upper bound.

Reported at **every** available horizon, because the fixed-horizon columns are not the 48h settlement deadline — a single number could be an artefact of the horizon chosen.

## Per horizon

| horizon | n (intrabar) | BRACKET_INTRA | SURVIVABLE | **ghost gap** | % of bracket | verdict |
|---|---:|---:|---:|---:|---:|---|
| **4h** | 12864 | 0.1681 | 0.0522 | **0.1158** | 68.9% | **NOT MET** |
| **24h** | 11893 | 0.2463 | 0.0570 | **0.1893** | 76.9% | **NOT MET** |
| **48h** | 10588 | 0.2588 | 0.0690 | **0.1897** | 73.3% | **NOT MET** |

Reference — close-walk bracket base rate (what the platform publishes): **0.2728** (n=41972).

## Verdict

The ghost gap is positive at **every** horizon (3/3), so this is not a horizon artefact.

**KILL CRITERION NOT MET at every horizon — the ghost edge survives on a consistent intrabar basis.** The close-walk label is genuinely optimistic, not a close-vs-intrabar artefact. Phase 1 stands: the intrabar resolver should be adopted.

## Cross-check against the earlier (confounded) figure

The earlier measurement compared a **close-walk** bracket label against an **intrabar** survivable label — mixing bases. Here both are intrabar.

- close-walk bracket: **0.2728** (published)
- intrabar bracket (24h): **0.2463** (-0.0265 vs close-walk)
- intrabar survivable (24h): **0.0570**

The basis change alone moves the base rate by **+0.0265**; the survival requirement accounts for the rest. The earlier 0.2805 → 0.1186 drop was those two effects stacked — this table separates them.

## Does the label choice change the discrimination verdict?

If AUC is at or below 0.5 under every label, the label debate is orthogonal to the model's ranking ability — which is the finding that matters for Phase 3.

| label | n | base rate | Brier | REL | RES | AUC |
|---|---:|---:|---:|---:|---:|---:|
| outcome=tp_hit (close-walk) | 41972 | 0.2728 | 0.2597 | 0.0616 | 0.0008 | **0.4861** |
| BRACKET_INTRA 4h | 12864 | 0.1681 | 0.2558 | 0.1164 | 0.0015 | **0.4605** |
| BRACKET_INTRA 24h | 11893 | 0.2463 | 0.2716 | 0.0873 | 0.0027 | **0.4491** |
| BRACKET_INTRA 48h | 10588 | 0.2588 | 0.2733 | 0.0828 | 0.0026 | **0.4587** |
| SURVIVABLE 4h | 12864 | 0.0522 | 0.2368 | 0.1865 | 0.0002 | **0.4880** |
| SURVIVABLE 24h | 11893 | 0.057 | 0.2428 | 0.1883 | 0.0001 | **0.4587** |
| SURVIVABLE 48h | 10588 | 0.069 | 0.2493 | 0.1845 | 0.0003 | **0.4394** |

**Read this row-wise, not as a ranking.** Each label is a different event with a different base rate, so Brier is not comparable across rows — the informative column is AUC, and the informative fact is whether it is above or below 0.5.

## Coverage

- fetched: **83809** light signals (all outcomes)
- close-walk label available: **41972** (50.1%)
- intrabar labels at 4h: **12864** (15.3% — needs `mfe_4h_pct` + geometry)
- intrabar labels at 24h: **11893** (14.2% — needs `mfe_24h_pct` + geometry)
- intrabar labels at 48h: **10588** (12.6% — needs `mfe_48h_pct` + geometry)

## Caveats

- **The bracket leg is reconstructed from `mfe_{h}_pct`**, not from a bar series (the table stores fixed-horizon excursions, not OHLC). 'MFE reached the TP distance' and 'first-touch TP' coincide when the excursion is over the same horizon and TP is the maximum favourable level — but they are not *identical* if price touched TP and then ran further while the stored MFE saturates. The MAE leg is the stored column directly, so the **survivable** label is the tighter of the two.
- **`mfe_4h_pct` has thin coverage** (added later than 24h/48h), so its n is smaller — read it as a sensitivity check, not the headline.
- **Fixed horizons are not the settlement deadline.** A trade reaching TP in hour 30 is 'not reached' at 24h and 'reached' at 48h; the gap is stable across both, which is why all three are reported.
- **Both legs now share the intrabar basis**, which is the point — but neither is a true bar-series first-touch until the resolver runs on OHLC.

