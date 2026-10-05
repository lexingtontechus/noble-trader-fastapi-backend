# Geometry Band Urgency (live) + Episode Dedup — 2026-10-04

**Trigger:** the nobletradingapp EOD report for Oct 3–4 read as a red flag —
"427 new qualified signals, 422 resolving, 2.6% realized win rate, −$3,619
for the day, cumulative −$8,418" with "mean predicted 44.7% vs realized
25.2%" over 7 days.

**Outcome:** the loss was real but the *magnitude was inflated ~10×* by a
measurement artifact. Three separate defects found; four fixes shipped.

---

## 1. Root cause: per-tick re-capture, not 422 independent bets

The light sweep runs every 15 min and writes an `nt_signal_sim` row on
**every tick** while a signal stays valid. One economic signal therefore
lands as N identical rows (same `symbol, signal, entry_price, take_profit,
stop_loss`), all resolving the same way.

Measured duplication (qualified rows per day):

| Date | qualified=true rows | distinct signals | duplication |
|---|---|---|---|
| 2026-09-30 | 188 | 170 | 1.1× |
| 2026-10-01 | 270 | 222 | 1.2× |
| 2026-10-02 | 196 | 196 | 1.0× |
| **2026-10-03 (Sat)** | **484** | **46** | **10.5×** |
| **2026-10-04 (Sun)** | **476** | **117** | **4.1×** |

Example — XAUAUD on Oct 3 wrote **75 identical rows** (`entry=5954.644,
tp=5864.27, sl=5999.83, atr=9.94, p_win_dir=0.9525`) between 04:46 and
07:31, one per sweep, every one resolving `sl_hit`. That is **one bet
counted 75 times**.

The EOD report aggregates on `.eq("qualified", true)` — raw rows — so its
win rate and P&L are a function of how long a signal stayed valid, not of
signal quality.

### Why Oct 3–4 were the worst

Forex/metals closed for the weekend (reopen Sun 22:00 UTC). The Oct 3–4
run coincided with a **Studio infra outage of 8h+**; when it came back the
sweep re-fired on the last-known bars, so the same signal was re-captured
for many consecutive ticks. Compounded by 24h markets (BTCUSD) still
trading and re-capturing normally.

Note: TDVA price itself is fine — verified live from the Studio during the
investigation, EURUSD/XAUUSD/XAUAUD/BTCUSD all at the current hour
(24.8 min old); AAPL correctly 51.9h (market closed).

## 2. Corrected numbers

Delivered tier (`gate_outcome='qualified'`, distinct signals):

| Date | report basis (raw rows) | delivered, distinct |
|---|---|---|
| 2026-10-02 | 196 rows | 123 signals, 8.1% WR, −$3,922 |
| 2026-10-03 | 484 rows, 2.6% WR, −$3,619 | **31 signals, 3.3% WR, −$938** |
| 2026-10-04 | 476 rows | **67 signals, 4.5% WR, −$1,126** |

7-day delivered basis: **805 resolved, 22.2% WR, −$5,437** vs the report's
raw-row −$19,773.

**The overconfidence is real** (delivered win rate genuinely ~3–8% against
44.7% predicted). The *count* was the artifact.

## 3. Downstream contamination

- `bracket_reachability._fetch_resolved_rows` had **no** `qualified` /
  `gate_outcome` filter — it ingested every duplicate, so every hit-rate
  bucket `n` and `pct_hit_*` was inflated, and the calibration bias built
  on it was poisoned.
- `calibration_bias_bucket` (both `build_calibration_bias_by_bucket` and
  `build_entry_drift`) had the same flaw.
- `nt_calibration_records` (built by the EOD resolver from the same rows)
  is duplicated 3.65× — feeds `v_eod_calibration_bias*`, i.e. the
  "13 overconfident / 1 underconfident" snapshot.

## 4. Fixes shipped

**Fix 1 — EOD report counts distinct signals** (`nobletradingapp`
`supabase/functions/eod-report-generate/index.ts`). Added `dedupeEpisodes()`
mirroring the backend; applied to the new-signal and resolved queries.
Win rate / P&L / benchmark now per-episode.

**Fix 2 — reachability + calibration tables count distinct signals**
(`bracket_reachability._dedupe_episodes`, applied in
`_fetch_resolved_rows`; same helper applied in `calibration_bias_bucket`
for both builders). Key = `(symbol, signal, regime, entry_price,
take_profit, stop_loss)` — the same "same signal" content the
orchestrator's own D5 content-hash dedup uses. `regime` is in the key
because a capture spanning a regime shift is a genuinely different
signal. Keeps the earliest row per episode.

**Fix 3 — price staleness detection** (`orchestrator._sweep_group`).
Replaces the legacy Alpaca market-hours gate. `price_staleness_max_hours:
1.0` (60 min — the 1h bar interval + slack; a live feed's newest bar is
0–60 min old). Symbols whose newest bar is older are skipped, so an
outage/weekend cannot generate signals on a frozen feed. Guarded against
empty/None-only timestamp lists.

**Fix 4 — geometry band urgency wired live.** The module existed but was
**never called** (no scheduler job, no sweep hook). Now runs at the END of
every light sweep via `_run_geometry_band_urgency` → `check_geometry_band_urgency`,
so bands stay current *between* qualified-signal generations, with a 4h
per-cell cooldown. Also fixed in the same pass:
- upsert payload used non-existent columns (`p_resolve`, `p_win_resolve`,
  `expectancy_r`) → would have failed every write; now uses the real
  `nt_bracket_reachability` columns.
- drift signal read from `nt_calibration_bias_by_cell` keyed correctly on
  `(side, vol_tier, distance_atr_bucket)`.
- rows episode-deduped before re-computation.
- migration 195 adds `geometry_band_urgency` to the `nt_eod_actions.action`
  CHECK (was not present → inserts would have failed 23514 silently).

## 5. Verification

- `tests/test_geometry_band_urgency.py` — 10 tests (episode dedup incl. the
  exact Oct-3 XAUAUD shape, earliest-row retention, distinct levels, NULL
  stop_loss, cross-day frozen feed; cell re-computation incl. side filter
  and missing-ATR skip). All pass.
- Full suite green (see commit).
- Studio verified running the deployed commit; calibration enforcement
  observed penalising metals (XAUAUD bias 0.577, XAUEUR 0.365).

## 6. Open items (not in this change)

- `nt_calibration_records` is still written per-capture by the resolver
  (3.65× duplication). The views reading it were not changed here —
  episode-counting for that table is a separate decision (its
  `v_eod_calibration_bias_latest` already merges overlapping
  `[entered_at, closed_at]` windows, migration 143, but
  `v_eod_calibration_bias` does not).
- Consider suppressing the per-tick capture itself (only write a new
  `nt_signal_sim` row when the signal content changes), which would fix
  the source rather than each consumer.
- `is_market_open()` / `sweep_after_hours` are now redundant for
  staleness purposes but left in place (crypto is genuinely 24h).
