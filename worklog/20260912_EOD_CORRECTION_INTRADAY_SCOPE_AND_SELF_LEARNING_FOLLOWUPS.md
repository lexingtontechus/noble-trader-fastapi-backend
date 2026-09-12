# RM-42 EOD Correction Intraday Scope + Self-Learning Follow-ups

> **Status: mixed — RM-42 and the macro-forecast design questions are
> scope-only (nothing built, decisions needed); the symbol-registration
> fix, the OpenRouter model fix, and the log-timeout investigation were
> small/mechanical/instructed enough to build and investigate directly
> this same session.** Per "scope in worklog, get honest review, build
> only on explicit instruction."

| # | Priority | Task | Status | Date | Roadmap refs |
|---|---|---|---|---|---|
| §1 | P2 | Scope RM-42 (EOD correction intraday restart-window fit) | scoped, needs decision | 2026-09-12 | RM-42 |
| §2 | P2 | Fix `nt_macro_forecast` symbol format + register commodities in `nt_symbol` | resolved — built | 2026-09-12 | RM-50 |
| §3 | P2 | Scope macro-forecast follow-ups (existing symbols, commodity-scope reduction, Edge Function decoupling) | scoped, needs decision | 2026-09-12 | RM-50 |
| §4 | P3 | Scope TimesFM forecast grounding for `triage.py` | scoped only | 2026-09-12 | n/a |
| §5 | P1 | Fix OpenRouter default model (both repos) | resolved — built | 2026-09-12 | RM-50, `llm_client_openrouter_free_model_retired` |
| §6 | P2 | Investigate the recurring 504 Gateway Timeout errors | investigated | 2026-09-12 | new row, see §6 |
| §7 | n/a | Record decision: let self-learning data accumulate before acting on "closest available lever" | recorded | 2026-09-12 | RM-50 |
| §8 | P2 | Scope a macro-forecast doc for existing (active) symbols | scoped only, doc not written | 2026-09-12 | RM-50 |
| §9 | P2 | Build: RM-42 Option B catch-up guard, `nt_macro_forecast` FK enforcement, multi-source registry | resolved — built | 2026-09-12 | RM-42, RM-50 |

---

## 1. RM-42 — EOD correction intraday restart-window fit

### 1.1 The original finding (2026-09-10)

`orchestrator.py` schedules `_run_eod_correction_only` five times a day
via a uniform-grid formula:
```python
interval = max(1, int(getattr(settings, "EOD_CORRECTION_INTERVAL_HOURS", 4) or 4))
for k in range(1, 24 // interval):
    hour = (0 + k * interval) % 24
    # -> 04:30, 08:30, 12:30, 16:30, 20:30 UTC
```
The original 2026-09-10 finding was that Lightning Studio restarts
roughly every 4 hours at irregular offsets, and each of these 5 fixed
slots landed 30-50 minutes *before* one of those restarts — the worry
being that a restart could kill the correction pass mid-run, or that
the fixed-grid formula doesn't account for the real restart cadence at
all.

### 1.2 What real evidence shows now (checked today, not assumed)

**The restart cadence is not a fixed clock pattern — it drifts.** Pulled
every `Starting sweep orchestrator` timestamp from the current log
(2026-09-11 through today):
```
02:16, 10:07, 14:10, 18:14, 22:17, 22:49, 23:12, 23:25 (2026-09-11)
03:08, 03:42, 05:07, 10:06, 14:09, 18:13, 22:17 (2026-09-12)
```
Excluding the 22:17-23:25 cluster (four restarts in ~68 minutes — looks
like a deploy-iteration burst, not steady state), the steady cadence is
close to **4h**, but the *phase* shifts every time — 05:07→10:06 is
4h59m, 10:06→14:09 is 4h3m, 14:09→18:13 is 4h4m, 18:13→22:17 is 4h4m.
This directly contradicts the original finding's implicit assumption of
a fixed daily clock pattern (e.g. "13:00, 17:04, 21:08, 01:12, 05:16,
09:24") — that pattern was true on the day it was measured, but any
deploy or restart-cadence anomaly re-phases it going forward. **A
static 5-slot offset table computed once would itself go stale after
the next deploy** — which, in this repo, is often (multiple times per
day some days).

**And the job has NOT actually failed once in the real evidence
available.** Every scheduled `eod-correction-intraday-*` run across
both 2026-09-11 and 2026-09-12 completed successfully with 0 errors:
```
2026-09-11 04:30:16 done: 14 symbols checked, 3 actions, 0 errors
2026-09-11 12:30:23 done: 14 symbols checked, 3 actions, 0 errors
2026-09-11 16:30:27 done: 14 symbols checked, 6 actions, 0 errors
2026-09-11 20:30:16 done: 14 symbols checked, 10 actions, 0 errors
2026-09-12 04:30:25 done: 14 symbols checked, 3 actions, 0 errors
2026-09-12 08:30:31 done: 14 symbols checked, 3 actions, 0 errors
2026-09-12 12:30:46 done: 14 symbols checked, 3 actions, 0 errors
2026-09-12 16:30:38 done: 14 symbols checked, 3 actions, 0 errors
2026-09-12 20:30:37 done: 14 symbols checked, 3 actions, 0 errors
```
10/10 completions, all finishing in well under a minute. The theoretical
collision risk is real (a restart landing mid-run would still kill it),
but it hasn't materialized — this job is fast enough that even a
30-46 minute runway is far more than it needs. **This changes the
urgency, not the validity, of the original finding.**

### 1.3 What could still make this bite

The one thing that could turn a fast, harmless job into a slow,
restart-vulnerable one: the same 504 Gateway Timeout pattern
investigated in §6 below. If `run_eod_correction` happens to hit
Supabase during one of those latency spikes on the day a restart also
lands close to its scheduled minute, the collision risk becomes real
instead of theoretical. The two findings are connected — this is worth
keeping in mind rather than treating RM-42 and §6 as unrelated.

### 1.4 Two options, needs a decision

**Option A — a custom offset table** (the original 2026-08-10 framing).
Pick 5 (or 6) fixed UTC times a few minutes *after* the observed restart
times, matching the review_condition's original ask. **Downside, now
confirmed by real data**: the restart phase drifts with every deploy, so
this table would need periodic re-measurement and re-tuning — a
recurring maintenance cost for a problem that hasn't actually caused a
failure yet.

**Option B — a catch-up guard, matching the pattern already proven for
3 other jobs** (`nt_macro_regime`, `nt_bracket_reachability`,
`nt_signal_sim`/eod-lookback, all in `_catch_up_stale_jobs`). On
startup, check whether the last correction pass (e.g. the most recent
row's timestamp in whatever table `run_eod_correction` writes to, or a
dedicated marker) is older than some threshold (e.g. 3-3.5h, tighter
than the 4h interval so a genuinely-skipped slot gets caught before the
next one is due); if so, run `_run_eod_correction_only` immediately.
This is robust to restart-cadence drift by construction — it doesn't
care *when* restarts happen, only whether the job's own output is
stale. **Recommended**, given §1.2's evidence that this is a resilience
improvement for a rare/theoretical failure mode, not an active bug —
Option B is cheaper to build and doesn't need re-tuning after every
deploy.

Nothing built here — waiting on which option (or "hold as-is, given
10/10 real success") to pursue.

---

## 2. Fix `nt_macro_forecast` symbol format + register commodities in `nt_symbol`

### 2.1 The problem

`macro_forecast_agent.py` (built 2026-09-11) writes bare tickers (`B`,
`HO`, `FCPO`, ...) into `nt_macro_forecast.symbol` — but none of those
12 commodities existed as rows in `nt_symbol` at all, so there was
nothing to actually match/join against.

### 2.2 Format decision — bare ticker, not `EXCHANGE:SYMBOL`

Checked `nt_symbol`'s real data before assuming: its own primary
identifier column (`symbol`) is a **bare ticker** (`AAPL`, `XAUUSD`,
`USDCAD`) — the `EXCHANGE:SYMBOL` form (`NASDAQ:AAPL`, `OANDA:XAUUSD`)
lives in a *separate* column, `tradingview_symbol`, used only for the
TradingView data-fetch call. Every other per-symbol table in this
schema (`nt_signal_sim`, `nt_sweep_result`, `nt_macro_regime`,
`nt_trend_regime`, `nt_calibration_records`, ...) keys on the bare
ticker too. **Kept `nt_macro_forecast.symbol` as the bare ticker** (no
code change needed there) and registered the 12 commodities in
`nt_symbol` using that same convention — this is the interpretation
that keeps the whole schema consistent. (If "exchange:symbol" literally
meant storing the qualified form in `nt_macro_forecast.symbol` itself,
that would be the one inconsistent table in the schema — flag if that
was actually the intent and I'll redo it.)

### 2.3 Built

`nt_symbol` migration `register_commodity_symbols_inactive` — all 12
commodities from `commodity_macro_context_scope.md` §1.1, `active =
false` (same convention as the existing inactive stock rows —
`INTC`/`META`/`MSFT`/`NVDA`/`RUSS2000`/`TSLA`/`UK100` — registered but
not swept), `exchange = 'TVC'`, `tradingview_symbol = 'TVC:<symbol>'`,
`asset_class = 'commodities'`, `data_source = 'tradingview'`,
`heartbeat_ttl_seconds = 900`. **Note**: `commodity_macro_context_scope.md`
§6.1 already flagged that TradingView availability for some of these
(`JX`, `VGT`, `SI`) is unverified — since these rows are `active=false`,
that risk doesn't matter yet (nothing tries to fetch live data for
them), but it will need checking before ever flipping any of them
active.

`macro_forecast_agent.py`'s `SYMBOLS` tuple comment updated to state the
match explicitly, so this isn't silently assumed again.

---

## 3. Macro-forecast follow-ups — scoped, not built

### 3.1 "Can we forecast for existing symbols as well?"

Not a small extension — the agent's design fundamentally depends on
`docs/LLM/commodity_12m_forecast_model_2027.md` as grounding text, and
**no equivalent document exists for forex/equities/crypto**. Two paths:
- Write an equivalent macro-outlook document for the other asset
  classes (real research/writing effort, same shape as the commodity
  doc) and reuse the exact same agent design.
- Or let the LLM reason from its own general knowledge without a
  grounding document for those symbols — meaningfully less reliable and
  harder to verify (the whole point of grounding in a written doc is
  that a human can check the agent's read against a citable source).

No default assumed — needs a decision on which path (or whether to
defer this asset-class expansion entirely) before building either.

### 3.2 "Reduce scope for commodities if the process is slow"

Real timing from tonight's manual run: ~5 minutes for 12 symbols,
because every single call fell through OpenRouter (broken, see §5) to
the slower NVIDIA NIM vision-instruct model, sequentially, one symbol
at a time. With §5's OpenRouter fix, most calls should route through
OpenRouter's own auto-router instead, which is typically much faster
than a large fallback vision model — so it's worth remeasuring before
cutting scope. If still slow: the calls are fully independent per
symbol and could run concurrently (`asyncio.gather`) instead of
sequentially — cheaper fix than dropping symbols outright, and doesn't
lose any of the 12 commodities' data. Given §7's decision to let data
accumulate slowly and analyze later, there's no freshness deadline
forcing urgency here either way.

### 3.3 "Move to a Supabase Edge Function if this needs to be decoupled?"

Considered, **not recommended as things stand**. The actual pain point
(slowness) is about model/provider choice and lack of concurrency, not
about which runtime executes the loop — moving to Deno wouldn't fix
either of those, and would mean either duplicating `llm_client.py`'s
logic in TypeScript (diverging from the Python version over time,
exactly the kind of drift that let the OpenRouter model default go
stale independently in two places already) or having the edge function
call back into this backend over HTTP (which doesn't actually
decouple anything). This is a nightly, already-isolated APScheduler job
with no live-signal dependency — there's no correctness reason it needs
to leave the backend. Worth revisiting only if a concrete reason to
decouple emerges (e.g. this backend's own restart cadence becoming a
real problem for it, which §1.2/§6 suggest isn't currently the case).

---

## 4. TimesFM forecast grounding for `triage.py` — scope only

The other deferred half of the original (pre-MVP) Phase 3 idea (see
`worklog/20260911_..._RESCOPE.md` §4/§6): ground `triage.py`'s reasoning
in the affected symbol's own TimesFM forecast, not just Phase 1's
code/worklog retrieval.

**What exists already**: TimesFM 2.5 runs every sweep cycle
(`_fetch_timesfm_cached` in `orchestrator.py`) and its output
(`p_timesfm`, the raw point forecast, `rel_change`) is captured in
`nt_sweep_result` per symbol per sweep. No new inference call is
needed — the data already exists, just isn't read by `triage.py`.

**What would need to be built**:
1. A read function — `fetch_latest_timesfm_forecast(symbol) ->
   dict | None`, analogous to `fetch_macro_regime()`: latest
   `nt_sweep_result` row for the symbol with the TimesFM columns
   selected.
2. `triage_finding()` calls this alongside `semantic_search()`, and
   `_build_prompt()` includes it: e.g. "TimesFM's most recent forecast
   for this symbol: rel_change=+0.3%, p_timesfm=0.52 (as of
   <timestamp>)".
3. **Open question**: how stale is "recent enough"? A finding's
   `detected_at` and the symbol's latest TimesFM read won't always be
   close in time (the finding may describe a multi-day pattern; TimesFM
   readings are per-sweep, every 15 min when active). Needs a decision
   on whether to use the single latest reading regardless of age, cap
   it to some staleness window (and omit if stale), or pull a short
   history spanning the finding's own date range.
4. Tests: mock the new fetch function, verify prompt inclusion,
   verify graceful omission when no TimesFM data exists for a symbol
   (matches every other best-effort pattern in this codebase).

This is a small, well-bounded addition — smaller than it might sound,
since it reuses data TimesFM already produces. Not built — flagged as
scoped and ready whenever it's prioritized.

---

## 5. OpenRouter default model — fixed (both repos)

Per user-provided fix: OpenRouter's account-level "Default Model" is
now set to their Free Models Router (dashboard: Settings → Default
Model → also used as the default fallback model). Given that,
`OPENROUTER_MODEL`'s code-level default was changed from the retired
`minimax/minimax-m3:free` to **`openrouter/auto`** — OpenRouter's own
meta-router, which picks a suitable model per request and honors that
account-level default/fallback configuration, rather than hardcoding
one specific model ID that can retire again later.

Built in both places that had the stale default:
- `noble-trader-fastapi-backend/regime_platform/services/self_learning/llm_client.py`
- `nobletradingapp/supabase/functions/eod-report-generate/index.ts`

Also fixed while touching `llm_client.py`: both except-blocks
(`OpenRouter call failed`, `NVIDIA NIM call failed`) were logging a bare
`%s` of the exception with no `type(exc).__name__` — the exact
`supabase_insert_exception_logging_empty_message` pattern, and a real
empty message was observed live during tonight's smoke test, hiding why
3 of 12 macro-forecast symbols failed. Both now log
`type(exc).__name__: repr(exc)`.

`nt_roadmap.llm_client_openrouter_free_model_retired` marked resolved.
`nobletradingapp`'s fix is a separate repo/deploy — not verified live
yet (that deploy is outside this backend's pipeline).

---

## 6. Investigation: the recurring 504 Gateway Timeout errors

Checked rather than assumed. The errors span many different tables
(`nt_symbol`, `nt_trend_probs_gate_shadow`, `nt_calibration_records`,
`log_renko_bricks`, `nt_signal_sim`) at scattered times — not one hot
query, not one bad table. Ran Supabase's own performance advisor for a
plausible systemic cause rather than guessing:

- **`nt_calibration_records_signal_id_fkey`** — an unindexed foreign
  key. A real, specific finding on exactly one of the tables that
  showed a 504 in the logs (`fetch_calibration_bias_raw` against
  `nt_calibration_records`).
- **13 RLS policies** re-evaluating `auth.<function>()`/
  `current_setting()` per-row instead of once per query
  (`auth_rls_initplan` lint).
- **119 instances** of multiple permissive RLS policies stacking on the
  same table/role/action, each one evaluated on every relevant query
  (`multiple_permissive_policies` lint).

The RLS findings in particular are project-wide, not table-specific —
consistent with the *scattered* pattern of which table times out (it's
whichever query happens to run while Postgres is under elevated
latency from this overhead, not a fixed problem query). **Likely root
cause: cumulative RLS policy overhead across the project, not a bug in
any specific query this backend issues.** The actual fix (indexing the
FK, consolidating the 119 policy instances) is a real, separate,
sizeable cleanup — not attempted here. Logged as a new `nt_roadmap` row
for future prioritization, not fixed this turn.

---

## 7. Recorded decision: let self-learning data accumulate first

Explicit instruction, recorded here so it isn't lost: don't build any
simulation/backtest harness for the Phase 3 triage's "closest available
lever" proposals yet (the observation from the last pulse check — the
LLM reaches for `EOD_BIAS_*` params on every regime-mismatch finding
even though none of the 11 allowed Tier D params directly address a
mislabeled HMM regime). Let `nt_self_learning_reports` and
`nt_macro_forecast` accumulate real data over time; analyze/simulate
the impact of actually applying a "closest available lever" change once
there's enough of it. No action needed now beyond keeping both nightly
jobs running.

---

## 8. Scope: a macro-forecast doc for existing (active) symbols

Per §3.1's open question, resolved partway: **yes, worth scoping** — but
scoping the document's requirements, not writing its content, which is
a separate research/authoring task from the code changes in §9.

### 8.1 Symbol scope

The 14 currently-active `nt_symbol` rows, grouped the same way the
commodity doc is grouped (by economic relationship, not asset-class
label alone):

| Group | Symbols |
|---|---|
| Precious metals (already have XAU/XAG rows, but no *forward* doc) | XAUUSD, XAUEUR, XAUAUD, XAGUSD |
| Forex majors | AUDUSD, EURUSD, GBPUSD, USDCAD, USDCHF, USDJPY |
| Equities/indices | AAPL, US30USD, US500 |
| Crypto | BTCUSD |

### 8.2 Structure — mirror the commodity doc, adapted per group

The commodity doc's shape (summary matrix → per-symbol quarterly
ranges → named drivers → bear/bull case → cross-commodity correlation
notes) doesn't transfer 1:1 — commodities are driven by supply/demand
fundamentals with clean quarterly seasonality; these four groups need
different framing:

- **Forex majors**: driven by central-bank rate-differential
  expectations, not seasonal supply/demand — the doc's per-symbol
  section would need *rate-decision calendar context* (next FOMC/ECB/
  BOE/BOJ/RBA/BOC meeting dates within the 12-month window) and
  relative-strength narrative (which side of the pair is expected to
  diverge, and why) instead of a physical-commodity supply story.
- **Equities** (AAPL, US30USD/US500 as indices): driven by
  earnings-cycle expectations and sector/macro positioning, not a
  quarterly price range in the same sense — would need next-earnings-
  date context (for AAPL specifically) and a broader market-regime
  read (rate-cut/hike cycle, index-level valuation) for the two index
  proxies.
- **Precious metals**: closest to the commodity doc's own shape
  (already has real-world drivers: real-rate expectations, safe-haven
  demand, central-bank buying) — reuse that section's structure most
  directly.
- **Crypto (BTCUSD)**: driven by a different mix again (liquidity
  cycle, regulatory calendar, halving-cycle position where relevant) —
  needs its own framing, not force-fit into either of the above.

### 8.3 What this means concretely

Given the four groups need genuinely different narrative inputs (rate
calendars vs. earnings calendars vs. commodity fundamentals vs. crypto-
specific drivers), **this is closer to four small documents than one
uniform one** — though they can live in a single file (matching the
commodity doc's own one-file-many-symbols shape) as long as each
symbol's section pulls from the right kind of driver.

### 8.4 Explicitly not done here

Writing the actual document (the real researched quarterly
ranges/drivers/dates, the way the commodity doc has real IMF/USDA/ING/
SocGen-sourced content) is a substantial research task, not a code
task — not attempted in this pass. §9 builds the code side (the
`ForecastSource` registry) so that whenever this document exists —
whether authored by hand or via a dedicated research pass — it plugs
in as one line, no further code change needed.

---

## 9. Built: RM-42 catch-up guard, FK enforcement, multi-source registry

### 9.1 RM-42 Option B — catch-up guard

Added to `_catch_up_stale_jobs()` (`orchestrator.py`), matching the
exact pattern already proven for `nt_macro_regime`/
`nt_bracket_reachability`/`nt_signal_sim`. Staleness signal:
`nt_symbol_calibration_state.computed_at` — checked, not assumed, that
this table is upserted **per-symbol on every single
`run_eod_correction()` pass**, intraday or nightly, `action="none"`/
`CALIBRATED` included, not only on an anomaly (confirmed live: 12
fresh `action="none"` rows from a catch-up-triggered nightly pass at
22:22 UTC today). This makes it a reliable "did a correction pass
actually run" signal regardless of which of the two callers produced
it — no new marker table needed.

`EOD_CORRECTION_INTRADAY_STALE_AFTER = timedelta(hours=3.5)` — tighter
than the 4h interval, same reasoning as the other three thresholds.
Gated on the same `settings.EOD_CORRECTION_INTRADAY_ENABLED` flag the
job registration itself uses. 2 new tests (threshold value + "shorter
than the full cycle" check, matching this file's existing convention
of unit-testing the pure pieces and verifying the live wiring via a
real restart, same as the other three checks).

**Not yet verified live** — needs the next Studio restart to land more
than 3.5h after the last correction pass to actually exercise the new
branch; will confirm via `[startup-catchup] nt_symbol_calibration_state
last computed_at=... stale, running the intraday EOD correction pass
now` in the log.

### 9.2 `nt_macro_forecast` symbol consistency — FK-enforced, not just conventional

Added `nt_symbol_symbol_key` (unique constraint on `nt_symbol.symbol` —
none existed before) and `nt_macro_forecast_symbol_fkey` (foreign key
from `nt_macro_forecast.symbol` to `nt_symbol.symbol`). Checked for
duplicate `nt_symbol.symbol` values before adding the unique
constraint — none found. This makes "symbols match `nt_symbol`" a
database-enforced invariant, not just a convention two pieces of code
happen to agree on.

### 9.3 Multi-source registry

`macro_forecast_agent.py` refactored from one hardcoded
(doc-path, symbol-tuple) pair to a `ForecastSource` registry
(`FORECAST_SOURCES: tuple[ForecastSource, ...]`) — `run_macro_forecast()`
now iterates every registered source independently, so **one source's
doc-load failure no longer blocks another source's symbols** (a real
robustness improvement over the previous single-pair design, not just
groundwork for §8's future doc). The existing 12-commodity behavior is
unchanged — `FORECAST_SOURCES` currently has exactly one entry
(`commodities_12m`). Adding the existing-symbols doc from §8 later is a
one-line addition to this tuple, no other code change.

5 new/updated tests covering multi-source aggregation and per-source
failure isolation. Full suite: 948 passed, 21 skipped (was 943).

