# Supabase Migrations — Ordering Notes

## Numbering gaps (L8, 2026-08-12)

The migration files are NOT strictly contiguous. Known gaps and why:

| Missing | Note |
|---|---|
| `033` | **Intentionally absent.** The 030–032 line (regime-shift + symbol plan assignment) was followed directly by 034; 033 was never shipped. |
| `035` | **Intentionally absent.** 034 (deprecate nt_pattern_stats) → 036 (TA indicators); 035 was never shipped. |
| `037` – `099` | **Reserved band.** The `0xx` series ends at 036; the `1xx` EOD/paper/analytics series begins at 100. The gap is a deliberate namespace split (feature-family series), not a data loss. |

## Series layout

- `001` – `032`: core schema + signal pipeline
- `034`, `036`: calibration deprecation + TA indicator columns (odd numbers
  skipped by design)
- `100` – `113`: EOD signal-lookback, paper portfolio, Talaria analytics,
  RLS hardening (this series is contiguous 100→113 except the 3-digit
  convention starts at 100)

## Apply order

PostgREST/Supabase applies by filename sort. The `1xx` series depends on the
`0xx` series having run first (e.g. `nt_calibration_records` is created in
100 but referenced by name in 024's guarded rename — 024 is safe because it
is guarded with `information_schema` checks, see migration 024 itself).

## Conventions

- `DROP POLICY IF EXISTS` before `CREATE POLICY` (no IF NOT EXISTS in SQL).
- New feature families start at the next `100`-band number.
- Never renumber applied migrations; document intentional gaps here instead.
