# Macro Forecast Scope — Existing Symbols in nt_symbol

**Status**: Draft, **reviewed and substantially corrected 2026-09-12** —
see §0.0 below before reading anything else in this file. The original
draft's Part 0 symbol inventory was wrong (conflated `_PRICE_DECIMALS`,
a code-level price-rounding lookup with entries for symbols that were
never actually registered, with real `nt_symbol` rows), which then
propagated into wrong symbol tables in Parts 1-4 and a false claim in
Part 5.4. The code-level references (file/line citations for
`macro_forecast_agent.py`, `macro_filter.py`, `orchestrator.py`) were
independently checked and are accurate — only the *database content*
claims were fabricated/stale.  
**Date**: 2026-09-15 (original draft)  
**Author**: Agent (noble-trader-fastapi-backend)  
**References**:
- `regime_platform/sweep_orchestrator/macro_filter.py` (247 lines — price-based 200-day SMA macro regime)
- `regime_platform/services/self_learning/macro_forecast_agent.py` (229 lines — LLM-based 12-month classification, existing multi-source `ForecastSource` registry)
- `regime_platform/core/feature_flags.py` (`macro_regime_mode: shadow`, `macro_forecast_agent_enabled: True`)
- `regime_platform/sweep_orchestrator/orchestrator.py` (`_run_macro_forecast_agent()` at line 2000, `_run_macro_regime_refresh()` at line 2112 — both verified accurate)
- `docs/LLM/commodity_12m_forecast_model_2027.md` (12 commodities — existing ForecastSource, `_COMMODITIES_SOURCE`)
- **`worklog/20260912_EOD_CORRECTION_INTRADAY_SCOPE_AND_SELF_LEARNING_FOLLOWUPS.md` §8** — scoped this exact same question 2026-09-12, using a real, verified `nt_symbol` query. That section's 14-symbol list is correct; this document should be read as a *elaboration* of that section's driver-framing analysis, not a competing/independent inventory. Reconcile before building anything from either.
- ~~`supabase/migrations/001_sim_tables.sql` (initial 27 symbols)~~ — **wrong, see §0.0**: migration 001 only creates the schema, it contains zero `INSERT INTO nt_symbol` statements.
- ~~`supabase/migrations/014_simplify_to_15_symbols.sql` (deactivated AVAX/ARB/DOGE/LINK/XRP/WIF/forex/metals, added SOLUSD, reactivated META)~~ — this migration is real and does what's described, but it operates on a table named `sim_symbols`, not `nt_symbol` (an old table name — `nt_symbol`'s `data_source` CHECK constraint is still literally named `sim_symbols_data_source_check` today, a rename artifact). It doesn't establish that AMZN/GOOGL/SPY/QQQ/BTC/ETH/SOL/ETHUSD/XAG/GOLD/SILVER ever existed as *currently live* `nt_symbol` rows — checked live, they don't (§0.0).
- `supabase/migrations/028_add_tradingview_datasource.sql` — real, and its content is narrower than originally described: adds the `tradingview_symbol` column, switches `BTCUSD` to TradingView/Coinbase, and inserts exactly 3 new rows (`USDJPY`, `EURUSD`, `XAUUSD`). It does not "add 27 symbols."
- ~~`regime_platform/sweep_orchestrator/orchestrator.py` `_PRICE_DECIMALS` (definitive current symbol list)~~ — **this was the root error, see §0.0**: `_PRICE_DECIMALS` is a static price-rounding lookup dict, a superset of every symbol ever priced by this codebase (including many never registered in `nt_symbol` at all, or long since deactivated) — it is NOT a live registry and was never a safe proxy for "currently active/registered symbols."

---

## Part 0.0: Correction — real `nt_symbol` inventory (verified 2026-09-12, not `_PRICE_DECIMALS`)

Queried directly rather than trusting the original draft's `_PRICE_DECIMALS`-derived list. `nt_symbol` currently has **33 rows total** — 21 active, 12 inactive (12 commodities registered 2026-09-12, `active=false`, unrelated to this scoping question):

| asset_class | symbol | exchange | active |
|---|---|---|---|
| commodities | XAUUSD, XAUEUR, XAUAUD, XAGUSD | OANDA | **true** |
| commodities | B, RB, HO, JX, VGT, FCPO, CT, SB, RR, ZC, ZW, SI | TVC | false (2026-09-12 registration, separate from this doc's scope) |
| crypto | BTCUSD | COINBASE | **true** |
| forex | AUDUSD, EURUSD, GBPUSD, USDCAD, USDCHF, USDJPY | OANDA | **true** |
| stocks | AAPL | NASDAQ | **true** |
| stocks | US30USD | OANDA | **true** |
| stocks | US500 | PEPPERSTONE | **true** |
| stocks | INTC, META, MSFT, NVDA, RUSS2000, TSLA, UK100 | NASDAQ/OANDA/OPOFINANCE | false |

**None of the following exist in `nt_symbol` at all — not active, not inactive, no row whatsoever** (checked directly, not inferred): `GOLD`, `SILVER`, `XAG`, `AMZN`, `GOOGL`, `SPY`, `QQQ`, `BTC` (bare), `ETH`, `SOL`, `ETHUSD`, `SOLUSD`, `AVAX`, `ARB`, `DOGE`, `LINK`, `XRP`, `WIF`, `ADA`, `MATIC`, `DOT`, `OIL`, `COPPER`. Every one of these appears somewhere in the original draft's Part 0-4 as if it were an existing, registered symbol — none of them are. `_PRICE_DECIMALS` has entries for most of these because it's a kitchen-sink formatting table, not a registry; the original draft's foundational assumption ("`_PRICE_DECIMALS` is the definitive list of currently registered symbols") was the single error that propagated through the rest of the document.

**Real consequence, not just a paperwork correction**: `nt_macro_forecast.symbol` now has a foreign-key constraint against `nt_symbol.symbol` (added 2026-09-12, `nt_macro_forecast_symbol_fkey`). Any `ForecastSource` entry built from this document's original Part 5.1 code (covering `GOLD`/`SILVER`/`AMZN`/`GOOGL`/`SPY`/`QQQ`/`BTC`/`ETH`/`SOL`/`ETHUSD`/`SOLUSD`/`XAG`) would fail at the database layer the first time `run_macro_forecast()` tried to upsert a row for any of them — not a hypothetical risk, a guaranteed `INSERT` failure against a real constraint.

The `macro_forecast_agent.py` file/line references, and the `ForecastSource` registry pattern itself, are accurate and unaffected by this correction — see Part 9.

**This scoping doc's real job, going forward**: cover the 21 real active symbols and the 12 real-but-inactive symbols (§0.1) — not a fictional 27+ symbol universe.

## Part 0.1: `nt_symbol.active` is irrelevant to macro forecasting — the commodities are the proof

Corrected mid-review (explicit instruction: "scope for non-active
symbols like commodities"). §0.0's framing above, and several
`Gaps`/driver notes in Parts 1-4, wrongly implied that an inactive
symbol (`META`/`MSFT`/`NVDA`/`TSLA`/`INTC`/`RUSS2000`/`UK100`) needs
**reactivating** before it can be macro-forecast-classified. Checked
against `macro_forecast_agent.py` itself: `run_macro_forecast()` /
`classify_symbol()` / `_run_source()` never read or check
`nt_symbol.active` anywhere — the only place `active` matters is
whether a symbol is picked up by `fetch_active_symbols()` for the
*live sweep* (light/heavy sweep, `macro_filter.py`'s own daily
`compute_macro_regime()` refresh, EOD resolution, etc.).

**The 12 commodities already prove this in production**: all 12 are
`active=false` (registered 2026-09-12 specifically so `nt_macro_forecast`'s
FK constraint would resolve, with no intention of sweeping them), and
`_COMMODITIES_SOURCE` classified 9/12 of them successfully in a real
smoke test the same day. Macro-forecast classification only needs two
things: (1) the symbol exists as an `nt_symbol` row (for the FK), and
(2) a `ForecastSource` doc that names it. `active` is irrelevant to
either requirement.

**Correction to apply throughout this document**: every "if
reactivated" / "would need reactivating first" qualifier attached to
`META`/`MSFT`/`NVDA`/`TSLA`/`INTC`/`RUSS2000`/`UK100` in Part 3 (and
the equivalent framing anywhere else) is **wrong as a precondition for
forecasting** — those 7 symbols can get a `ForecastSource` entry and
be classified into `nt_macro_forecast` today, exactly like the
commodities, with zero live-trading exposure, since `active=false`
already guarantees the sweep/live path never touches them. Reactivation
is a *separate*, unrelated decision (whether to actually trade the
symbol) — not a dependency of this scoping question.

**Revised real symbol universe for this doc, accounting for this**:
all **33** `nt_symbol` rows are eligible for macro-forecast coverage
right now — the 21 active ones (already scoped in
`worklog/20260912_..._FOLLOWUPS.md` §8) and the 12 inactive ones (the
12 commodities, already covered by `_COMMODITIES_SOURCE`, **plus** the
7 inactive stocks this document's Part 3 originally mis-scoped as
needing reactivation first: `META`, `MSFT`, `NVDA`, `TSLA`, `INTC`,
`RUSS2000`, `UK100`).

---

## Part 1: Precious Metals

### Symbols

**Corrected 2026-09-12**: only the 4 OANDA rows are real, active `nt_symbol`
entries (`asset_class = 'commodities'`, not `precious_metals` — there is
no `precious_metals` asset_class value in this schema). `GOLD`, `SILVER`,
and `XAG` (COMEX/NYMEX futures-style tickers) do **not exist in
`nt_symbol`** — struck through, kept only to show what the original
draft wrongly assumed existed.

| symbol | tradingview_symbol | exchange | asset_class | active |
|--------|-------------------|----------|-------------|--------|
| XAUUSD | OANDA:XAUUSD | OANDA | commodities | true |
| XAUEUR | OANDA:XAUEUR | OANDA | commodities | true |
| XAUAUD | OANDA:XAUAUD | OANDA | commodities | true |
| XAGUSD | OANDA:XAGUSD | OANDA | commodities | true |
| ~~GOLD~~ | ~~GC=F~~ | — | — | **does not exist** |
| ~~SILVER~~ | ~~SI=F~~ | — | — | **does not exist** |
| ~~XAG~~ | ~~OANDA:XAG~~ | — | — | **does not exist** |

### Macro Driver: Real Rates + USD + Risk-Off Flow

Precious metals are driven by a fundamentally different mechanism than commodities or equities. The primary driver is **real interest rates** (nominal rate minus inflation expectations), which determines the opportunity cost of holding non-yielding assets. Secondary drivers: USD strength (inverse correlation), central bank buying (structural demand from China/India/Russia), geopolitical risk premium (risk-off flows into gold), and physical supply (mining output, recycling).

**Rate calendars matter here differently than for forex.** A Fed rate cut expectation doesn't just move gold — it moves the *entire curve* and changes the carry cost of holding gold vs. TIPS. The key data points: FOMC decisions, CPI/PCE prints, 10Y TIPS yields, DXY. Unlike forex where the *differential* between two currencies matters, gold responds to the *absolute level* of real rates.

**Gold vs. Silver divergence**: Silver has ~60% industrial demand (solar panels, electronics) — it behaves like a hybrid between precious metal and cyclical commodity. Gold is ~90% monetary/jewelry demand. During risk-off events, gold outperforms silver significantly. During industrial expansions, silver can outperform gold. The Gold/Silver ratio is itself a macro indicator.

**Priced in USD**: All precious metals trade in USD. A stronger dollar makes gold more expensive for foreign buyers, suppressing demand. The DXY is the most important cross-asset reference.

### Current Macro Filter Mechanism

`macro_filter.py` computes `macro_label` (bull/bear/neutral) from `last_close vs. 200-day SMA` with EMA smoothing. For XAUUSD, this means: is gold (in USD terms) above or below its 200-day moving average? The `kelly_macro_multiplier` then adjusts effective kelly by ±`boost_scale`/`penalty_scale` (default 0.3/0.5) based on alignment.

This is purely price-based. It doesn't capture the *reason* XAUUSD is above its 200-day SMA (real rates falling? USD weakening? geopolitical risk?). The macro forecast agent fills this gap with qualitative classification.

### Forecast Model Structure

| Quarter | Key Drivers | XAUUSD Outlook | XAUEUR Outlook | XAUAUD Outlook | XAGUSD Outlook |
|---------|------------|-----------------|-----------------|-----------------|-----------------|
| Q4 2026 | Fed rate path, CPI trend, DXY | | | | |
| Q1 2027 | Real yield trajectory, central bank buying | | | | |
| Q2 2027 | Inflation expectations, geopolitical risk | | | | |
| Q3 2027 | Cycle positioning, industrial demand (silver) | | | | |

### Key Data Sources
- **FRED**: 10Y TIPS yield, DXY, CPI, PCE
- **OANDA (via TradingView)**: XAUUSD, XAUEUR, XAUAUD, XAGUSD — the 4 real, active symbols. (`GC=F`/`SI=F` COMEX futures and a bare `XAG` symbol do not exist in `nt_symbol` — struck from the original draft's data-source list; no code currently fetches them.)
- **World Gold Council**: Central bank purchases, ETF flows
- **COT Report**: Managed money positioning in COMEX gold/silver

### Existing `macro_filter.py` Integration
- `compute_macro_regime()` fetches daily bars via `fetch_tradingview_batch()` → computes 200-day SMA → EMA smoothing → `macro_label`/`macro_strength`
- `upsert_macro_regime()` writes to `nt_macro_regime`
- `macro_forecast_agent.py` adds LLM-based classification to `nt_macro_forecast` (separate table)
- Both run on their own cadence — macro_filter daily, forecast agent daily at 02:45 UTC

### Gaps
- No real-time TIPS yield integration (could be a `dynamic_numerical_covariate` for TimesFM)
- No central bank purchase data feed
- No Gold/Silver ratio tracking
- XAUUSD/XAUEUR/XAUAUD/XAGUSD on OANDA may have different data availability than COMEX futures

---

## Part 2: Forex Majors

### Symbols

**Corrected 2026-09-12**: the original draft omitted `USDCHF` — a real,
active `nt_symbol` row — entirely. All 6 below are real and active.

| symbol | tradingview_symbol | exchange | asset_class | active |
|--------|-------------------|----------|-------------|--------|
| EURUSD | OANDA:EURUSD | OANDA | forex | true |
| GBPUSD | OANDA:GBPUSD | OANDA | forex | true |
| USDJPY | OANDA:USDJPY | OANDA | forex | true |
| AUDUSD | OANDA:AUDUSD | OANDA | forex | true |
| USDCAD | OANDA:USDCAD | OANDA | forex | true |
| USDCHF | OANDA:USDCHF | OANDA | forex | true |

### Macro Driver: Interest Rate Differentials + Central Bank Divergence

Forex is driven by **relative monetary policy** — the interest rate differential between two economies determines carry trade flows and capital allocation. Unlike commodities (supply/demand fundamentals) or equities (earnings growth), forex is a **pure relative-value asset class**. The absolute level of rates matters less than the *differential* and the *expected path of divergence*.

**Rate calendars are the single most important driver.** Every FOMC, ECB, BOJ, RBA, and BOC decision moves these pairs. But it's not just the decision — it's the *forward guidance* and *dot plot*. A "hold" decision with hawkish guidance can move EURUSD more than a hike with dovish guidance.

**Key differentials by pair**:
- **EURUSD**: Fed vs. ECB — the largest and most liquid pair. Driven by US-EU rate differential, growth differential (GDP, PMI), and risk sentiment (EUR is a risk currency, USD is a safe haven).
- **GBPUSD**: Fed vs. BOE — GBP is the "wild card" — smaller, more volatile, sensitive to UK-specific political risk (Brexit legacy, fiscal credibility). Higher beta to risk sentiment than EURUSD.
- **USDJPY**: Fed vs. BOJ — the most asymmetric pair. BOJ is the only major central bank still running negative/zero rates (or very low). USDJPY is essentially a carry trade — it moves on the *divergence* between US and Japanese policy. BoJ intervention risk is unique.
- **AUDUSD**: Fed vs. RBA — AUD is a **commodity currency** (iron ore, coal, LNG exports to China). Driven by China growth, commodity prices, and RBA policy. AUDUSD often correlates with gold (both are risk-sensitive, both benefit from weaker USD).
- **USDCAD**: Fed vs. BOC — CAD is an **oil currency** (Canada is a major oil exporter). USDCAD correlates inversely with crude oil prices. Driven by oil fundamentals, BOC policy, and US-Canada trade dynamics.
- **USDCHF** (added 2026-09-12 — real, active symbol missing from the original draft): Fed vs. SNB — CHF is the archetypal **safe-haven currency**, alongside JPY. The SNB has a long history of active intervention to manage franc strength (it directly hurts Swiss export competitiveness), including past currency-peg episodes — SNB policy statements and FX-intervention signals matter more here than for any other pair in this set. USDCHF tends to fall (CHF strengthens) during global risk-off events, the opposite of AUDUSD/commodity-currency behavior.

**DXY (US Dollar Index)** is the master variable — all pairs are correlated to it. DXY itself is driven by the Fed's policy relative to the rest of the world.

### Current Macro Filter Mechanism

Same `compute_macro_regime()` pattern: 200-day SMA, EMA smoothing, `macro_label`/`macro_strength`. For EURUSD, this means: is EURUSD above or below its 200-day moving average? The SMA captures the *trend* but not the *cause*.

### Forecast Model Structure

| Quarter | Key Drivers | EURUSD | GBPUSD | USDJPY | AUDUSD | USDCAD | USDCHF |
|---------|------------|--------|--------|--------|--------|--------|--------|
| Q4 2026 | Fed/ECB/SNB divergence, DXY trajectory | | | | | | |
| Q1 2027 | Rate path convergence/divergence, growth | | | | | | |
| Q2 2027 | Inflation trajectory, labor market | | | | | | |
| Q3 2027 | Cycle positioning, political risk | | | | | | |

### Key Data Sources
- **Central Bank Calendars**: FOMC, ECB, BOJ, RBA, BOC, **SNB** decision dates
- **FRED**: Fed Funds Rate, ECB Deposit Rate, BOJ Policy Rate, **SNB Policy Rate**, US CPI, EU CPI, Japan CPI
- **OANDA (via TradingView)**: `OANDA:EURUSD`, `OANDA:GBPUSD`, `OANDA:USDJPY`, `OANDA:AUDUSD`, `OANDA:USDCAD`, `OANDA:USDCHF` — the real `tradingview_symbol` format, not a `TICKER=X` Yahoo-style suffix (corrected; the original draft's `EURUSD=X` etc. form doesn't match what's actually stored)
- **Bloomberg/Reuters**: Forward rate expectations, OIS curves
- **COT Report**: Speculative positioning in CME forex futures

### Existing `macro_filter.py` Integration
- Same pattern as precious metals — `compute_macro_regime()` fetches daily bars via `fetch_tradingview_batch()`
- `fetch_tradingview_batch()` already supports `tradingview_symbol` format (OANDA:EURUSD, etc.) from migration 028
- The `forex` asset_class is recognized by `is_market_open()` in orchestrator.py (line 2375)

### Gaps
- No interest rate differential data feed (need OIS curves or central bank projections)
- No DXY index in the current symbol set (could add as a reference)
- No forward rate expectations data
- COT positioning data not integrated
- AUDUSD and USDCAD have commodity correlations that the current SMA-based macro filter doesn't capture
- USDCHF's safe-haven correlation to risk sentiment (opposite sign to AUDUSD's) isn't captured either

---

## Part 3: Equities

### Symbols

**Corrected 2026-09-12**: `AMZN`, `GOOGL`, `SPY`, `QQQ` **do not exist**
in `nt_symbol` at all — these need new registration before anything
else. `META`/`MSFT`/`NVDA`/`TSLA` (plus `INTC`, `RUSS2000`, `UK100`,
not covered by the original draft at all) **do exist, inactive** — and
per §0.1, `active=false` is **not** a blocker for macro-forecast
coverage; all 7 are just as forecastable today as `AAPL`, using the
same `ForecastSource` pattern the commodities already prove out.
`US30USD`/`US500` (OANDA/PEPPERSTONE index CFDs, `asset_class='stocks'`
in this schema) are the real, active symbols filling the
"broad-market benchmark" role `SPY`/`QQQ` were meant to play below —
genuinely similar *purpose*, structurally different *instrument* (a
CFD on the index, not the ETF itself).

| symbol | tradingview_symbol | exchange | asset_class | active | macro-forecastable now? |
|--------|-------------------|----------|-------------|--------|--------------------------|
| AAPL | NASDAQ:AAPL | NASDAQ | stocks | true | yes |
| US30USD | OANDA:US30USD | OANDA | stocks | true | yes |
| US500 | PEPPERSTONE:US500 | PEPPERSTONE | stocks | true | yes |
| META | NASDAQ:META | NASDAQ | stocks | false | **yes — active not required (§0.1)** |
| MSFT | NASDAQ:MSFT | NASDAQ | stocks | false | **yes** |
| NVDA | NASDAQ:NVDA | NASDAQ | stocks | false | **yes** |
| TSLA | NASDAQ:TSLA | NASDAQ | stocks | false | **yes** |
| INTC | NASDAQ:INTC | NASDAQ | stocks | false | **yes** |
| RUSS2000 | OPOFINANCE:RUSS2000 | OPOFINANCE | stocks | false | **yes** |
| UK100 | OANDA:UK100 | OANDA | stocks | false | **yes** |
| ~~AMZN~~ | ~~AMZN~~ | — | — | **does not exist** | no — needs registering first |
| ~~GOOGL~~ | ~~GOOGL~~ | — | — | **does not exist** | no — needs registering first |
| ~~SPY~~ | ~~SPY~~ | — | — | **does not exist** | no — needs registering first |
| ~~QQQ~~ | ~~QQQ~~ | — | — | **does not exist** | no — needs registering first |

### Macro Driver: Earnings Cycle + Monetary Policy + Growth Expectations

Equities are driven by a fundamentally different mechanism than forex or commodities. The primary driver is **corporate earnings growth expectations** — discounted future cash flows. But the *discount rate* (determined by monetary policy) is equally important. This creates a **dual-engine** structure:

1. **Earnings engine**: Revenue growth, margin expansion, guidance revisions, sector rotation. Driven by the business cycle, consumer spending, enterprise IT investment, AI capex (currently the dominant theme for NVDA/MSFT — **both real `nt_symbol` rows, currently inactive; per §0.1 that doesn't block macro-forecast coverage, only live trading**).
2. **Discount rate engine**: Fed policy, real yields, risk premium. Higher rates compress P/E multiples, especially for growth stocks (long-duration assets). Lower rates expand multiples.

**Rate calendars matter differently for equities than for forex.** For forex, rate differentials drive the pair directly. For equities, the *level* of rates matters more than the *differential* — it's about the discount rate applied to future earnings. A 5% Fed rate is a headwind for equities regardless of what ECB does; a 2% Fed rate is a tailwind.

**Earnings season is the dominant catalyst.** Quarterly earnings reports (Jan/Apr/Jul/Oct) create predictable volatility patterns. Guidance revisions (not just actual results) drive price action for the symbol reporting them — `AAPL`, `META`, `MSFT`, `NVDA`, `TSLA` are all real `nt_symbol` rows and all equally macro-forecastable today regardless of `active` status (§0.1); only `AAPL` currently feeds the live sweep/macro_filter path.

**Sector rotation is a macro signal**: In late-cycle expansion, rotate from growth to value (financials, energy). In recessions, rotate to defensives (utilities, staples — not in the current symbol set at all, active or not). `US30USD`/`US500` (real, active index CFDs) fill the "broad-market beta benchmark" role this draft originally assigned to `SPY`/`QQQ`, which don't exist here.

**NVDA is a special case** (real `nt_symbol` row, currently inactive — per §0.1 this doesn't block macro-forecast coverage, only its participation in the live sweep): it's simultaneously an equity (earnings-driven) and a macro indicator (AI capex cycle) — its earnings move the broader market, not just its own price. This is worth classifying into `nt_macro_forecast` regardless of activation status.

### Current Macro Filter Mechanism

Same `compute_macro_regime()` pattern. For AAPL, this means: is AAPL above or below its 200-day SMA? The 200-day SMA captures the long-term trend but doesn't distinguish between earnings-driven moves and rate-driven moves.

### Forecast Model Structure

| Quarter | Key Drivers | AAPL | US30USD | US500 |
|---------|------------|------|---------|-------|
| Q4 2026 | Earnings season, Fed path, AI capex | | | |
| Q1 2027 | GDP growth, margins, guidance | | | |
| Q2 2027 | Consumer spending, rates, cycle | | | |
| Q3 2027 | Political/regulatory risk, cycle | | | |

*(A second table covering `META`/`MSFT`/`NVDA`/`TSLA`/`INTC`/`RUSS2000`/`UK100` can be built today, independent of activation status — see §0.1 — just not drafted in this pass.)*

### Key Data Sources
- **Earnings calendars**: Company-specific earnings dates, consensus estimates (Zacks, Bloomberg) — relevant to `AAPL`, `META`, `MSFT`, `NVDA`, `TSLA` all equally (activation status doesn't change what data a forecast doc needs to cover)
- **FRED**: Fed Funds Rate, 10Y Treasury, GDP, CPI, Consumer Confidence, PMI
- **OANDA/PEPPERSTONE (via TradingView)**: `OANDA:US30USD`, `PEPPERSTONE:US500` — corrected from the original draft's fictional `SPY`/`QQQ` (via Yahoo) sourcing
- **COT Report**: Speculative positioning in equity index futures (ES, NQ)
- **VIX**: Volatility index — risk-off signal for equities
- **Sector ETFs**: XLK, XLF, XLE, XLV (not in `nt_symbol` at all — useful for rotation analysis only as external reference data, not as tradeable symbols here)

### Existing `macro_filter.py` Integration
- Same pattern — `fetch_tradingview_batch()` fetches daily bars for all `stocks` asset_class symbols
- `annualise_factor = 252` for stocks (vs 8760 for crypto) in `_process_symbol()`
- `is_market_open()` (`market_hours.py`, called from `orchestrator.py:2375`) filters out stocks outside market hours
- `US30USD`/`US500` serve as the real market-benchmark symbols in this system — their macro regime does not automatically propagate to `AAPL` or any other stock; each symbol's SMA is computed independently. This is orthogonal to `active` status — `macro_filter.py`'s own daily refresh only runs for `active=true` symbols in the first place (it reads `fetch_active_symbols()`), so an inactive stock has no `nt_macro_regime` row to propagate *to* regardless.

### Gaps
- No earnings data integration (EPS, revenue, guidance — the dominant equity driver)
- No VIX or volatility data (critical for equity risk assessment)
- No sector rotation indicators
- No Fed "dot plot" or forward guidance parsing
- `US30USD`/`US500` macro regime doesn't propagate to `AAPL` (or any other stock) automatically — each symbol's SMA is independent
- `META`/`MSFT`/`NVDA`/`TSLA`/`INTC`/`RUSS2000`/`UK100` are real rows, inactive — but per §0.1 that's not a blocker for `macro_forecast_agent.py` coverage, only for `macro_filter.py`'s own SMA-based path (which does require `active=true`, since it reads `fetch_active_symbols()`). The two mechanisms have genuinely different eligibility rules — worth keeping straight.
- NVDA's macro-indicator status isn't captured by the current price-only SMA filter (and can't be, for an inactive symbol — `macro_filter.py` never runs against it; `macro_forecast_agent.py` can still classify it)

---

## Part 4: Crypto

### Symbols

**Corrected 2026-09-12**: `BTCUSD` is the **only** crypto symbol that
exists in `nt_symbol` — active, `COINBASE` exchange, `tradingview_symbol
= 'COINBASE:BTCUSD'` (migration 028 switched it from Alpaca to
TradingView). `BTC`, `ETH`, `SOL`, `ETHUSD`, `SOLUSD` do not exist at
all — no Hyperliquid or Alpaca crypto rows are currently registered.
The ETH/SOL-specific commentary below (BTC dominance rotation,
altcoin correlation) is kept as general crypto-market background
useful if those symbols are ever added, but nothing in this codebase
currently trades or classifies them.

| symbol | tradingview_symbol | exchange | asset_class | active |
|--------|-------------------|----------|-------------|--------|
| BTCUSD | COINBASE:BTCUSD | COINBASE | crypto | true |
| ~~BTC~~ | ~~BTC~~ | — | — | **does not exist** |
| ~~ETH~~ | ~~ETH~~ | — | — | **does not exist** |
| ~~SOL~~ | ~~SOL~~ | — | — | **does not exist** |
| ~~ETHUSD~~ | ~~ETHUSD~~ | — | — | **does not exist** |
| ~~SOLUSD~~ | ~~SOLUSD~~ | — | — | **does not exist** |

### Macro Driver: Liquidity Cycle + Regulatory Events + On-Chain Adoption

Crypto is driven by a fundamentally different mechanism than traditional assets. The primary driver is **global liquidity conditions** — crypto behaves like a high-beta risk asset that responds to the same forces as equities but with amplified magnitude. Secondary drivers: regulatory events (SEC actions, ETF approvals), on-chain adoption metrics (active addresses, TVL, transaction volume), and halving cycles (BTC supply shock every 4 years).

**Rate calendars matter differently for crypto than for equities.** Crypto doesn't have earnings or central bank policy directly. Instead, it responds to the *liquidity implications* of rate decisions. When the Fed cuts rates → liquidity expands → risk assets rally → crypto outperforms. When the Fed hikes → liquidity contracts → risk assets sell off → crypto underperforms more severely than equities (higher beta).

**The key difference from equities**: crypto's correlation to risk assets is *state-dependent*. In risk-on environments, BTC correlates strongly with SPY/NVDA (both are liquidity-sensitive). In risk-off environments, BTC sometimes decouples as a "digital gold" narrative emerges (though this correlation is weak and inconsistent). The dominant regime is risk-on/risk-off, not gold-like safe haven.

**Regulatory events are crypto-specific catalysts** that don't exist for traditional assets:
- SEC vs. Coinbase/Ripple/Binance lawsuits
- ETF approvals (BTC spot ETF in Jan 2024, ETH spot ETF in 2024-2025)
- Stablecoin regulation (USDC, USDT)
- Tax policy changes
- These events can cause 10-30% moves within hours — the dominant source of idiosyncratic volatility

**Halving cycles**: BTC halving (next: ~2028) reduces new supply by 50%. Historically, halvings precede bull runs by 6-12 months. The halving is a *supply-side* shock that interacts with *demand-side* liquidity conditions.

**On-chain metrics as leading indicators**: Active addresses, transaction volume, TVL (Total Value Locked), stablecoin supply, exchange net flows. These provide fundamental adoption data that doesn't exist for traditional assets. The "crypto-specific" macro driver is adoption velocity.

**BTC dominance**: BTC's share of total crypto market cap tends to rise in risk-off (flight to quality within crypto) and fall in risk-on (capital rotates to altcoins like ETH/SOL). BTC dominance is itself a macro indicator for the crypto market.

### Current Macro Filter Mechanism

Same `compute_macro_regime()` pattern — 200-day SMA, EMA smoothing. For BTCUSD, this means: is BTCUSD above or below its 200-day moving average? The `annualise_factor = 8760` (24/7 market) vs 252 for stocks is the key difference in the Kelly sizing calculation.

Crypto trades 24/7/365 — no market hours filter. This means the macro regime is computed on continuous data without gaps, unlike equities which have weekend/holiday gaps.

### Forecast Model Structure

| Quarter | Key Drivers | BTCUSD |
|---------|------------|--------|
| Q4 2026 | Fed liquidity, ETF flows, regulatory | |
| Q1 2027 | Adoption metrics, stablecoin supply | |
| Q2 2027 | Halving cycle positioning, on-chain | |
| Q3 2027 | Cycle positioning, regulatory clarity | |

*(No `ETH`/`SOL` columns — neither exists in `nt_symbol`. Would need
new symbol registration first, a separate decision from this doc.)*

### Key Data Sources
- **FRED**: Fed Funds Rate, M2 Money Supply (the most important crypto macro indicator), CPI
- **OANDA/COINBASE (via TradingView)**: `COINBASE:BTCUSD` — the one real symbol
- **On-chain data**: Glassnode, Dune Analytics (active addresses, TVL, exchange flows, stablecoin supply)
- **Regulatory calendar**: SEC/CFTC decisions, ETF approval dates, legislative events
- **BTC Halving Schedule**: Next halving ~April 2028
- **CoinDesk/Cointelegraph**: Regulatory and adoption news

### Existing `macro_filter.py` Integration
- Same `compute_macro_regime()` pattern — `fetch_tradingview_batch()` fetches daily bars
- `annualise_factor = 8760` for crypto (24/7 market)
- `is_market_open()` returns True for crypto at all times (no market hours filter)
- `macro_regime_mode = "shadow"` for `BTCUSD` (same as all new symbols)
- `BTCUSD` on Coinbase via TradingView — the only crypto data path currently wired up (no Hyperliquid/Alpaca crypto rows exist)

### Gaps
- No M2 money supply data feed (the most important crypto macro indicator)
- No on-chain metrics integration (active addresses, TVL, exchange flows)
- No regulatory event calendar
- No BTC halving schedule tracking
- No BTC dominance tracking (moot with only one crypto symbol registered)
- No stablecoin supply data
- No correlation analysis with equities (risk-on/risk-off regime detection)
- Crypto-specific volatility (80%+ annualized) makes the 200-day SMA filter less effective than for traditional assets
- Only one crypto symbol exists at all — `ETH`/`SOL` would need registering from scratch, not just reactivating

---

## Part 5: Implementation Requirements

### 5.1 New `ForecastSource` Entries for `macro_forecast_agent.py`

**Corrected 2026-09-12** — every symbol below now only lists symbols
that actually exist in `nt_symbol` (verified §0.0); per §0.1,
`active=false` symbols are included on equal footing with active ones,
since the agent doesn't check that column. The existing
`_COMMODITIES_SOURCE` pattern needs to be extended. The
`FORECAST_SOURCES` tuple at line 84 currently contains only
`_COMMODITIES_SOURCE`. Add three new sources:

```python
_PRECIOUS_METALS_SOURCE = ForecastSource(
    name="precious_metals_12m",
    doc_path=_DOCS_LLM_DIR / "precious_metals_12m_forecast_model_2027.md",
    symbols=("XAUUSD", "XAUEUR", "XAUAUD", "XAGUSD"),  # GOLD/SILVER/XAG don't exist in nt_symbol
)

_FOREX_MAJORS_SOURCE = ForecastSource(
    name="forex_majors_12m",
    doc_path=_DOCS_LLM_DIR / "forex_majors_12m_forecast_model_2027.md",
    symbols=("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"),  # USDCHF added -- real symbol, omitted from the original draft
)

_EQUITIES_SOURCE = ForecastSource(
    name="equities_12m",
    doc_path=_DOCS_LLM_DIR / "equities_12m_forecast_model_2027.md",
    # AMZN/GOOGL/SPY/QQQ don't exist in nt_symbol -- excluded, not just renamed.
    # META/MSFT/NVDA/TSLA/INTC/RUSS2000/UK100 are real but inactive -- included
    # anyway per §0.1 (active is irrelevant to macro-forecast eligibility).
    symbols=("AAPL", "US30USD", "US500", "META", "MSFT", "NVDA", "TSLA", "INTC", "RUSS2000", "UK100"),
)

_CRYPTO_SOURCE = ForecastSource(
    name="crypto_12m",
    doc_path=_DOCS_LLM_DIR / "crypto_12m_forecast_model_2027.md",
    symbols=("BTCUSD",),  # the only crypto symbol that exists -- BTC/ETH/SOL/ETHUSD/SOLUSD don't
)

FORECAST_SOURCES: tuple[ForecastSource, ...] = (
    _COMMODITIES_SOURCE,
    _PRECIOUS_METALS_SOURCE,
    _FOREX_MAJORS_SOURCE,
    _EQUITIES_SOURCE,
    _CRYPTO_SOURCE,
)
```

No other code changes needed — `run_macro_forecast()` iterates all sources automatically.

If `AMZN`/`GOOGL`/`SPY`/`QQQ`/`ETH`/`SOL`/`GOLD`/`SILVER` are ever
wanted, they need a real `nt_symbol` registration migration first
(matching `register_commodity_symbols_inactive`'s pattern) — a
separate, explicit decision, not a byproduct of writing a forecast doc.

### 5.2 Forecast Model Documents to Create

| File | Symbols (real, corrected) | Key Drivers | Size |
|------|---------|------------|------|
| `docs/LLM/precious_metals_12m_forecast_model_2027.md` | XAUUSD, XAUEUR, XAUAUD, XAGUSD | Real rates, USD, central bank buying, geopolitical risk | ~5KB |
| `docs/LLM/forex_majors_12m_forecast_model_2027.md` | EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF | Rate differentials, central bank divergence, DXY | ~5KB |
| `docs/LLM/equities_12m_forecast_model_2027.md` | AAPL, US30USD, US500, META, MSFT, NVDA, TSLA, INTC, RUSS2000, UK100 | Earnings cycle, discount rate, AI capex, index-CFD benchmarks | ~8KB |
| `docs/LLM/crypto_12m_forecast_model_2027.md` | BTCUSD | Liquidity cycle, regulatory, halving, on-chain | ~3KB (single symbol) |

Each follows the same structure as `commodity_12m_forecast_model_2027.md` — quarterly outlook + named risk drivers per instrument.

### 5.3 System Prompt Adaptation in `macro_forecast_agent.py`

The `_SYSTEM_PROMPT` (line 95-114) is commodity-specific ("12-month forecast model", "quarterly outlook"). Needs to be generalized or have per-source system prompts. The `_build_prompt()` function (line 122) already accepts `doc_text` dynamically, so the prompt template is source-agnostic — just the `_SYSTEM_PROMPT` header needs to be flexible.

Options:
- **Option A**: One `_SYSTEM_PROMPT` that mentions all four asset classes and their driver framing
- **Option B**: Per-source `_SYSTEM_PROMPT` (commodity, metals, forex, equities, crypto) passed as a parameter to `classify_symbol()`
- **Option C**: The `_SYSTEM_PROMPT` already works if the `doc_text` contains the right framing — the LLM infers the asset class from the document content

**Option C is preferred** — the document itself provides the context, no code changes needed.

### 5.4 Supabase `nt_symbol` Entries — most exist, some don't (corrected 2026-09-12)

**The original claim ("all symbols are already registered... from
migration 001") was false** — migration 001 creates the schema only,
zero symbol inserts (§0.0). Real status, verified by direct query:

- **Already exist, real asset_class values** (`commodities`/`forex`/`stocks`/`crypto` — there is no `precious_metals` value in this schema): `XAUUSD`/`XAUEUR`/`XAUAUD`/`XAGUSD` (commodities, active); `EURUSD`/`GBPUSD`/`USDJPY`/`AUDUSD`/`USDCAD`/`USDCHF` (forex, active); `AAPL`/`US30USD`/`US500` (stocks, active); `META`/`MSFT`/`NVDA`/`TSLA`/`INTC`/`RUSS2000`/`UK100` (stocks, inactive — usable per §0.1); `BTCUSD` (crypto, active).
- **Do NOT exist — need a real registration migration before they can appear in any `ForecastSource`** (the FK added 2026-09-12 will reject an upsert otherwise): `GOLD`, `SILVER`, `XAG`, `AMZN`, `GOOGL`, `SPY`, `QQQ`, `ETH`, `SOL`, `ETHUSD`, `SOLUSD`.

No `asset_class` relabeling needed for the real rows — they're already correctly labeled.

### 5.5 `_PRICE_DECIMALS` Already Complete

All existing symbols are already in `_PRICE_DECIMALS` in `orchestrator.py` — no additions needed.

### 5.6 `macro_filter.py` No Changes Needed

`macro_filter.py` is symbol-agnostic — it fetches daily bars for whatever `fetch_active_symbols()` returns and computes `compute_macro_regime()` per symbol. The existing mechanism works for all asset classes. The only asset-class-specific logic is `annualise_factor = 8760 if asset_class == "crypto" else 252` in `orchestrator.py::_process_symbol()`, which is unrelated to the macro filter.

### 5.7 `feature_flags.yaml` No Changes Needed

`macro_regime_mode` and `macro_forecast_agent_enabled` are already set. No new feature flags needed — the existing `ForecastSource` registry pattern handles all four asset classes.

---

## Part 6: Driver Framing Summary (Per Asset Class)

| Asset Class | Primary Driver | Rate Calendar Role | Secondary Drivers | Unique Factor |
|------------|---------------|-------------------|-------------------|---------------|
| **Precious Metals** | Real interest rates | Fed/CPI → real yields | USD, central bank buying, geopolitical risk | Gold/Silver ratio, DXY inverse |
| **Forex Majors** | Interest rate differentials | Fed/ECB/BOJ divergence | Growth differentials, risk sentiment | Carry trade, central bank intervention |
| **Equities** | Earnings cycle + discount rate | Fed policy → P/E compression/expansion | AI capex cycle, consumer spending, guidance | NVDA as macro indicator, sector rotation |
| **Crypto** | Global liquidity cycle | Fed → M2 → risk-on/risk-off | Regulatory events, halving, on-chain adoption | 24/7 market, BTC dominance, stablecoin supply |

---

## Part 7: Verification Plan

### Acceptance Criteria
1. Four forecast model documents created in `docs/LLM/`
2. `macro_forecast_agent.py` `FORECAST_SOURCES` tuple extended with 4 new `ForecastSource` entries
3. `run_macro_forecast()` successfully classifies all symbols across all 5 sources (**corrected**: 12 commodities + 4 precious metals + 6 forex + 10 equities + 1 crypto = **33 symbols** — matches `nt_symbol`'s real total row count exactly, since this now covers every existing symbol, active or not)
4. `nt_macro_forecast` table populated with classification rows for all new symbols
5. Shadow mode validation: compare LLM-based `current_macro_label` with `macro_filter.py`'s `macro_label` for divergence — **only possible for the 21 active symbols**, since inactive ones never get an `nt_macro_regime` row to compare against (§0.1) — not a blocker, just means 12 of the 33 symbols get forecast-only coverage with no SMA baseline to diff against yet
6. Full pytest suite passes
7. No changes to `macro_filter.py`, `feature_flags.py`, `_PRICE_DECIMALS`, or `nt_symbol` schema — **true for existing rows; a separate migration IS needed first for `GOLD`/`SILVER`/`XAG`/`AMZN`/`GOOGL`/`SPY`/`QQQ`/`ETH`/`SOL`/`ETHUSD`/`SOLUSD` if any of those are wanted later**

### Tests
- `test_macro_forecast_agent_sources.py`: Verify all 5 `ForecastSource` entries load correctly
- `test_macro_forecast_agent_classify.py`: Verify `classify_symbol()` works with each source's document
- `test_forecast_source_symbols.py`: Verify all symbols in `FORECAST_SOURCES` exist in `nt_symbol`
- `test_macro_forecast_agent_shadow.py`: Verify shadow mode logging of divergence between LLM and SMA labels

---

## Part 8: Implementation Sequence

| Phase | Task | Duration | Dependencies |
|-------|------|----------|-------------|
| 1 | Create `precious_metals_12m_forecast_model_2027.md` | 2 days | This scoping doc |
| 2 | Create `forex_majors_12m_forecast_model_2027.md` | 2 days | This scoping doc |
| 3 | Create `equities_12m_forecast_model_2027.md` | 2 days | This scoping doc |
| 4 | Create `crypto_12m_forecast_model_2027.md` | 2 days | This scoping doc |
| 5 | Extend `FORECAST_SOURCES` in `macro_forecast_agent.py` | 1 day | Phases 1-4 |
| 6 | Test `run_macro_forecast()` with all sources | 1 day | Phase 5 |
| 7 | Shadow mode validation | 2 weeks | Phase 6 |
| 8 | Update `commodity_macro_context_scope.md` if needed | 0.5 day | All |
| 9 | Commit and push | 0.5 day | All |

**Total estimated duration**: 3-4 weeks (excluding research time)

---

## Part 9: Key Code References

### `macro_forecast_agent.py` (229 lines)
- **`ForecastSource` dataclass** (line 54-58): `name`, `doc_path`, `symbols` — the registry pattern
- **`_COMMODITIES_SOURCE`** (line 72-76): Existing commodities source — the template to follow
- **`FORECAST_SOURCES`** (line 84-86): Tuple of all sources — extend this
- **`_SYSTEM_PROMPT`** (line 95-114): LLM classification instructions — commodity-specific header
- **`classify_symbol()`** (line 159-173): One symbol → one `nt_macro_forecast` row
- **`run_macro_forecast()`** (line 213-229): Iterates all sources, classifies all symbols, upserts

### `macro_filter.py` (247 lines)
- **`compute_macro_regime()`** (line 62-112): Price-based 200-day SMA macro regime — symbol-agnostic
- **`refresh_macro_regime()`** (line 151-219): Daily job — fetches daily bars, computes, upserts to `nt_macro_regime`
- **`kelly_macro_multiplier()`** (line 115-139): Kelly adjustment based on macro label alignment

### `orchestrator.py`
- **`_run_macro_forecast_agent()`** (line 2000-2017): Calls `run_macro_forecast()` — already wired
- **`_PRICE_DECIMALS`** (line 601): a price-rounding lookup dict, **not a symbol registry** (the error corrected throughout this document, §0.0) — it has stale/superset entries for symbols never in, or long removed from, `nt_symbol`. Use a live query against `nt_symbol` for anything requiring "which symbols currently exist," never this dict.
- **`annualise_factor = 8760 if asset_class == "crypto" else 252`** (line 2754-2755): Asset-class-specific annualization

### `feature_flags.py`
- **`macro_regime_mode`**: `shadow` — existing symbols already use this
- **`macro_forecast_agent_enabled`**: `True` — agent already enabled

---

## Part 10: Reconciliation with `worklog/20260912_..._FOLLOWUPS.md` §8

That section scoped the same question first, correctly, from a live
query (14 active symbols, grouped by driver type — precious metals,
forex majors, equities, crypto). This document's corrected version now
covers the same ground in more depth, **plus** the 12 real-but-inactive
symbols (12 commodities + 7 inactive stocks) that §8 didn't count
since it only queried `active=true` at the time. The two aren't in
conflict once this document's Part 0 is corrected — treat this
document as the canonical, detailed version going forward; §8 remains
useful as the original, narrower real-data source this one was checked
against.

**Still not done, by design**: no forecast-model documents have been
written (Part 5.2 is a spec, not content) and `FORECAST_SOURCES` has
not been extended in code — this pass corrected the *scope*, it did
not build anything. Building requires: (1) explicit approval, (2) a
decision on whether `GOLD`/`SILVER`/`AMZN`/`GOOGL`/`SPY`/`QQQ`/`ETH`/`SOL`
are wanted enough to warrant a new `nt_symbol` registration migration,
separate from just writing the docs for what already exists.

---

*End of macro forecast scope for existing symbols. Reviewed and
corrected 2026-09-12 — see §0.0/§0.1 for what changed and why.*
