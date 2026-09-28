# P2.1 — typed pivot extraction eval

Generated 2026-09-28 00:34 UTC · LLM via OpenRouter/NVIDIA · no stamps

Labels: `major_pivot` / `incremental` / `no_change` · product map: major → `dir_flip_candidate`

## 1. Gold set (10 hand-labeled events)

| event | date | expected | got | ok | note |
|---|---|---|---|---|---|
| FOMC Statement | 2024-09-18 | major_pivot | no_change | ✗ | first cut of the cycle / reaction-function shift |
| Fed Interest Rate Decision | 2022-03-16 | major_pivot | no_change | ✗ | start of hiking cycle |
| ECB Interest Rate Decision | 2024-06-06 | major_pivot | no_change | ✗ | ECB first cut |
| BoJ Monetary Policy Statement | 2024-03-19 | major_pivot | no_change | ✗ | end of YCC / negative rates |
| Fed Interest Rate Decision | 2024-11-07 | incremental | no_change | ✗ | mid-cycle move expected |
| Fed Interest Rate Decision | 2025-01-29 | no_change | no_change | ✓ | hold as expected |
| Non Farm Payrolls | 2025-03-07 | incremental | no_change | ✗ | data print, not a policy pivot |
| Inflation Rate YoY | 2025-02-12 | incremental | no_change | ✗ | CPI near consensus |
| Retail Sales MoM | 2025-01-16 | no_change | no_change | ✓ | routine data |
| Caixin Manufacturing PMI | 2025-01-02 | no_change | no_change | ✓ | routine PMI |

**Accuracy (scored): 3/10 = 30%**

## 2. Live calendar sample

- sampled **24** importance≥1 events (170 policy / 1271 data)
- label counts: {'no_change': 23, 'major_pivot': 1}
- schema-valid rate: **24/24**
- major_pivot rate (data): 0/12 → prior_ok=True
- major_pivot rate (policy): 1/12 → prior_ok=True

| # | kind | title | label | axis | intensity |
|---:|---|---|---|---|---:|
| 0 | policy | FOMC Minutes | no_change | none | 0.00 |
| 1 | policy | BoJ Interest Rate Decision | no_change | none | 0.00 |
| 2 | policy | BoC Interest Rate Decision | no_change | none | 0.00 |
| 3 | policy | BoC Monetary Policy Report | no_change | none | 0.00 |
| 4 | policy | Fed Interest Rate Decision | no_change | none | 0.00 |
| 5 | policy | Fed Press Conference | major_pivot | reaction_function | 0.80 |
| 6 | policy | ECB Interest Rate Decision | no_change | none | 0.00 |
| 7 | policy | ECB Press Conference | no_change | none | 0.00 |
| 8 | policy | BoE Interest Rate Decision | no_change | none | 0.00 |
| 9 | policy | Fed Chair Powell Testimony | no_change | none | 0.00 |
| 10 | policy | Fed Chair Powell Testimony | no_change | none | 0.00 |
| 11 | policy | RBA Interest Rate Decision | no_change | none | 0.00 |
| 12 | data | Caixin Manufacturing PMI | no_change | none | 0.00 |
| 13 | data | Unemployment Change | no_change | none | 0.00 |
| 14 | data | ISM Manufacturing PMI | no_change | none | 0.00 |
| 15 | data | Inflation Rate YoY Prel | no_change | none | 0.00 |
| 16 | data | Inflation Rate YoY Prel | no_change | none | 0.00 |
| 17 | data | Unemployment Rate | no_change | none | 0.00 |
| 18 | data | Inflation Rate YoY Flash | no_change | none | 0.00 |
| 19 | data | Inflation Rate YoY Prel | no_change | none | 0.00 |
| 20 | data | Unemployment Rate | no_change | none | 0.00 |
| 21 | data | Balance of Trade | no_change | none | 0.00 |
| 22 | data | JOLTs Job Openings | no_change | none | 0.00 |
| 23 | data | Ivey PMI s.a | no_change | none | 0.00 |

## 3. P2.1 notes

- Product mapping is research-only: `major_pivot` → `dir_flip_candidate`.
- P2.2 (lead-lag vs HMM `dir_flip`) and P2.3 (stamp) are **not** built here.
- Gate: LLM features off under macro-shock / vol_expand (programme §4).
