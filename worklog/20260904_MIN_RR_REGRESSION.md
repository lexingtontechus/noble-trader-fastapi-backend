# 2026-09-04 — MIN_RR/MAX_SHARPE/MIN_TRADES silently tightened 08-30, fixed

> **Priority**: P0 · **Task**: Fix silently tightened
> MIN_RR/MAX_SHARPE/MIN_TRADES config values blocking forex/crypto
> signal qualification ·
> **Status**: resolved · **Date**: 2026-09-04

## Trigger

User observation: no new qualified crypto or forex signals since
08/31. Investigated via `nt_signal_sim`'s `qualified` history per
symbol plus recent `nt_sweep_result` gate-failure reasons.

## Finding: two separate, independent causes

**1. `min_rr`/`heavy_sweep_grid` regression (the actual root cause of
the timing)**

`config/feature_flags.yaml` shipped `min_rr: 2.0`,
`heavy_sweep_grid.max_sharpe: 10.0`, `heavy_sweep_grid.min_trades: 10`
in commit `dbce327` (2026-08-30, "Move MIN_RR/MAX_SHARPE/MIN_TRADES off
env vars onto feature_flags.yaml"). Those are the module's hard-coded
*fallback defaults* — not the Studio's actual long-running values.
Confirmed live via `ssh ... env`: the Studio process still has
`MIN_RR=1.5`, `HEAVY_SWEEP_MAX_SHARPE=5.0`, `HEAVY_SWEEP_MIN_TRADES=5`
set in its environment today, all three now doing nothing, since
`feature_flags.py` is deliberately designed to never consult
environment variables (see that module's own header) — the switch from
`os.getenv("MIN_RR", "2.0")` to `get_min_rr()` silently dropped the
real override the moment it deployed.

That commit's own message claimed *"Verified: all three resolve to the
same defaults as before... not a correction to bad live data"* — that
verification checked the three modules' hard-coded defaults agreed with
each other, not that they matched what the Studio was actually running.
They didn't.

Net effect: the light-sweep qualification gate's RR requirement
silently went from 1.5 to 2.0 for three-plus weeks — landing exactly in
the gap orchestrator.py's own comment already flagged: *"with default
brick params sl_bricks=3, tp_bricks=5, the actual RR is 5/3 = 1.67 —
below the 2.0 minimum."* Any symbol trading at or near default brick
params would silently and permanently fail to qualify.

Evidence this explains the drought: `nt_sweep_result` over the last 3
days — USDCAD (400/717 ticks with positive Kelly), USDJPY (268/713),
BTCUSD (208/713) all get a real, positive Kelly regularly, yet **zero**
qualified in that window. Combined with `HEAVY_SWEEP_MAX_SHARPE`
tightening 5.0→10.0 (this direction is looser, not stricter — lets more
combos through, so not itself blocking anything) and
`HEAVY_SWEEP_MIN_TRADES` tightening 5→10 (stricter — could silently
exclude otherwise-valid combos for symbols with a smaller backtested
trade count), the combined effect on the heavy sweep's combo selection
plausibly compounds the light-sweep RR effect for these symbols.

**2. EURUSD/GBPUSD: separate, unrelated, not a regression**

Both show `kelly_f` at bit-for-bit exactly `0.0000` for 3 straight
days — `position_sizer.py`'s Kelly clips negative edge to 0, and `mu`
(mean log-return over the fit window) has been persistently negative
for both (`mu=-0.0000145` EURUSD, `mu=-0.0000385` GBPUSD). This is not
new: `PositionSizer.size()` computes `mu` from raw price returns and
never receives the trade `direction` at all — `_process_symbol` doesn't
compute `direction` until well after `kelly_result` is already built.
A symbol in sustained decline can therefore never qualify a **short**
either, since Kelly reads the downward drift as "no edge" rather than
"edge for a short." Longstanding characteristic, not something that
changed on 08-30 — flagged here for visibility, not fixed in this pass
(a real fix would mean threading `direction` into Kelly sizing before
it runs, a bigger, separate-scope change).

## Fix (this pass)

Restored `config/feature_flags.yaml` to the values actually running on
the Studio before the regression:
- `min_rr: 2.0` -> `1.5`
- `heavy_sweep_grid.max_sharpe: 10.0` -> `5.0`
- `heavy_sweep_grid.min_trades: 10` -> `5`

Chosen specifically because these are the Studio's own long-tuned,
already-live values (not a fresh guess) — restoring them corrects the
regression precisely rather than picking a new number.

**Not done**: the dead `MIN_RR`/`HEAVY_SWEEP_MAX_SHARPE`/
`HEAVY_SWEEP_MIN_TRADES` Studio Secrets still exist and do nothing —
worth deleting from the Studio's environment config directly (not
something reachable via this repo or plain SSH) to avoid future
confusion, but leaving that to the user rather than guessing at how
their Lightning AI Studio secret management is set up.

**Not done**: the `position_sizer.py` direction-blind Kelly calculation
(EURUSD/GBPUSD's issue) — flagged, not scoped or built here.

## Confirmed live post-restart (2026-09-04, same day)

Studio restarted 00:59:09 UTC with this fix deployed — startup log
confirms `MIN_RR=1.5` (was `2.0` on the prior restart, 22:18:00 UTC the
day before, which predates this fix's push and still showed 2.0).

Verified via direct query, not just taking it at face value: **BTCUSD
qualified at 01:40:18 UTC** — its first qualification since
2026-08-30 20:30:19, ~5 days stale before this fix. Direct confirmation
the restored threshold is working for at least one of the affected
symbols. EURUSD/GBPUSD/USDCAD/USDJPY hadn't produced a new qualified
signal in the first ~hour post-restart — not itself concerning given
how infrequently any of them qualify even when healthy (a handful of
times per day at most) — worth one more check-in in a few hours to
confirm those four specifically resume.
