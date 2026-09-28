# P2.3 — narrative / pivot labels → dir_flip lead-lag

Generated 2026-09-28 04:40 UTC · unique events=2 (major=0) · past=0 future_dropped=2 · flips=4457

T0 = `events[]` / `pivot_as_of` (LLM-typed), **not** raw calendar rows.

**Note:** `events[]` currently holds **upcoming** calendar rows (often future `as_of`). Lead-lag needs **historical** labeled narrative/pivot events — re-run as those accumulate or after a backfill pass.

## 1. Flip intensity after labeled events (rate-normalized)

| channel | window | n flips | exposure h | flips/h | lift |
|---|---|---:|---:|---:|---:|
| all labeled | 0-6h | 0 | 0 | nan | **nan×** |
| all labeled | 6-24h | 0 | 0 | nan | **nan×** |
| all labeled | 24-72h | 0 | 0 | nan | **nan×** |
| all labeled | 72-168h | 0 | 0 | nan | **nan×** |
| major_pivot | 0-6h | 0 | 0 | nan | **nan×** |
| major_pivot | 6-24h | 0 | 0 | nan | **nan×** |
| major_pivot | 24-72h | 0 | 0 | nan | **nan×** |
| major_pivot | 72-168h | 0 | 0 | nan | **nan×** |

Baseline flip intensity: **5.1779/h**.

## 2. Hours from last labeled T0 to flip

- **any**: no prior T0
- **major_pivot**: no prior T0
- **incremental**: no prior T0

## 3. P2.3 read

If **major_pivot** lift ≫ baseline and concentrated in 0–6h/6–24h, text is a useful **confirm/prior**.
If lift ≈ 1 everywhere, narrative does **not** lead this book — keep P2.3 stamps gated.

Research-only. No gate/size change.
