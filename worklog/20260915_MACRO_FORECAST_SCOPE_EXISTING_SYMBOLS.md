# Macro Forecast Scope — Existing Symbols in nt_symbol

**Status**: Draft  
**Date**: 2026-09-15  
**Author**: Agent (noble-trader-fastapi-backend)  
**References**:
- `regime_platform/sweep_orchestrator/macro_filter.py` (247 lines — price-based 200-day SMA macro regime)
- `regime_platform/services/self_learning/macro_forecast_agent.py` (229 lines — LLM-based 12-month classification, existing multi-source `ForecastSource` registry)
- `regime_platform/core/feature_flags.py` (`macro_regime_mode: shadow`, `macro_forecast_agent_enabled: True`)
- `regime_platform/sweep_orchestrator/orchestrator.py` (`_run_macro_forecast_agent()` at line 2000, `_run_macro_regime_refresh()` at line 2112)
- `supabase/migrations/001_sim_tables.sql` (initial 27 symbols), `028_add_tradingview_datasource.sql` (BTCUSD→COINBASE, USDJPY/EURUSD/XAUUSD inserts)
- `supabase/migrations/014_simplify_to_15_symbols.sql` (deactivated AVAX/ARB/DOGE/LINK/XRP/WIF/forex/metals, added SOLUSD, reactivated META)
- `docs/LLM/commodity_12m_forecast_model_2027.md` (12 commodities — existing ForecastSource, `_COMMODITIES_SOURCE`)
- `regime_platform/sweep_orchestrator/orchestrator.py` `_PRICE_DECIMALS` (definitive current symbol list)

---

## Part 0: Current Symbol Inventory (from `_PRICE_DECIMALS` and migrations)

The `_PRICE_DECIMALS` dict in `orchestrator.py` is the definitive list of currently registered symbols:

**Precious Metals**: GOLD, SILVER, XAUUSD, XAUEUR, XAUAUD, XAGUSD, XAG
**Forex Majors**: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD
**Equities**: AAPL, AMZN, GOOGL, META, MSFT, NVDA, TSLA, SPY, QQQ
**Crypto**: BTC, ETH, SOL, BTCUSD, ETHUSD, SOLUSD

Additionally, from migrations: AVAX, DOT, LINK, MATIC, ADA (deactivated crypto), ARB, DOGE, XRP, WIF (deactivated).

The `macro_forecast_agent.py` already has a `ForecastSource` registry pattern — `_COMMODITIES_SOURCE` points at `docs/LLM/commodity_12m_forecast_model_2027.md` with symbols `("B", "RB", "HO", "JX", "VGT", "FCPO", "CT", "SB", "RR", "ZC", "ZW", "SI")`. The comment at line 78-83 explicitly says: "Existing-symbol (forex/equities/crypto) macro-outlook document — scoped in worklog 20260912_..._FOLLOWUPS.md §8, not yet written. Add a second ForecastSource entry here."

**This scoping doc is that second ForecastSource.**

---

## Part 1: Precious Metals

### Symbols
| symbol | tradingview_symbol | exchange | asset_class | price_precision |
|--------|-------------------|----------|-------------|-----------------|
| GOLD | GC=F | NYMEX/COMEX | precious_metals | 2 |
| SILVER | SI=F | NYMEX/COMEX | precious_metals | 2 |
| XAUUSD | OANDA:XAUUSD | OANDA | precious_metals | 3 |
| XAUEUR | OANDA:XAUEUR | OANDA | precious_metals | 4 |
| XAUAUD | OANDA:XAUAUD | OANDA | precious_metals | 4 |
| XAGUSD | OANDA:XAGUSD | OANDA | precious_metals | 4 |
| XAG | OANDA:XAG | OANDA | precious_metals | 4 |

### Macro Driver: Real Rates + USD + Risk-Off Flow

Precious metals are driven by a fundamentally different mechanism than commodities or equities. The primary driver is **real interest rates** (nominal rate minus inflation expectations), which determines the opportunity cost of holding non-yielding assets. Secondary drivers: USD strength (inverse correlation), central bank buying (structural demand from China/India/Russia), geopolitical risk premium (risk-off flows into gold), and physical supply (mining output, recycling).

**Rate calendars matter here differently than for forex.** A Fed rate cut expectation doesn't just move gold — it moves the *entire curve* and changes the carry cost of holding gold vs. TIPS. The key data points: FOMC decisions, CPI/PCE prints, 10Y TIPS yields, DXY. Unlike forex where the *differential* between two currencies matters, gold responds to the *absolute level* of real rates.

**Gold vs. Silver divergence**: Silver has ~60% industrial demand (solar panels, electronics) — it behaves like a hybrid between precious metal and cyclical commodity. Gold is ~90% monetary/jewelry demand. During risk-off events, gold outperforms silver significantly. During industrial expansions, silver can outperform gold. The Gold/Silver ratio is itself a macro indicator.

**Priced in USD**: All precious metals trade in USD. A stronger dollar makes gold more expensive for foreign buyers, suppressing demand. The DXY is the most important cross-asset reference.

### Current Macro Filter Mechanism

`macro_filter.py` computes `macro_label` (bull/bear/neutral) from `last_close vs. 200-day SMA` with EMA smoothing. For GOLD, this means: is gold above or below its 200-day moving average? The `kelly_macro_multiplier` then adjusts effective kelly by ±`boost_scale`/`penalty_scale` (default 0.3/0.5) based on alignment.

This is purely price-based. It doesn't capture the *reason* gold is above its 200-day SMA (real rates falling? USD weakening? geopolitical risk?). The macro forecast agent fills this gap with qualitative classification.

### Forecast Model Structure

| Quarter | Key Drivers | Gold Outlook | Silver Outlook | XAUUSD Outlook |
|---------|------------|-------------|----------------|----------------|
| Q4 2026 | Fed rate path, CPI trend, DXY | | | |
| Q1 2027 | Real yield trajectory, central bank buying | | | |
| Q2 2027 | Inflation expectations, geopolitical risk | | | |
| Q3 2027 | Cycle positioning, industrial demand (silver) | | | |

### Key Data Sources
- **FRED**: 10Y TIPS yield, DXY, CPI, PCE
- **TradingView**: GC=F, SI=F daily bars
- **OANDA**: XAUUSD, XAUEUR, XAUAUD, XAGUSD, XAG
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
| symbol | tradingview_symbol | exchange | asset_class | price_precision |
|--------|-------------------|----------|-------------|-----------------|
| EURUSD | OANDA:EURUSD | OANDA | forex | 5 |
| GBPUSD | OANDA:GBPUSD | OANDA | forex | 5 |
| USDJPY | OANDA:USDJPY | OANDA | forex | 3 |
| AUDUSD | OANDA:AUDUSD | OANDA | forex | 5 |
| USDCAD | OANDA:USDCAD | OANDA | forex | 5 |

### Macro Driver: Interest Rate Differentials + Central Bank Divergence

Forex is driven by **relative monetary policy** — the interest rate differential between two economies determines carry trade flows and capital allocation. Unlike commodities (supply/demand fundamentals) or equities (earnings growth), forex is a **pure relative-value asset class**. The absolute level of rates matters less than the *differential* and the *expected path of divergence*.

**Rate calendars are the single most important driver.** Every FOMC, ECB, BOJ, RBA, and BOC decision moves these pairs. But it's not just the decision — it's the *forward guidance* and *dot plot*. A "hold" decision with hawkish guidance can move EURUSD more than a hike with dovish guidance.

**Key differentials by pair**:
- **EURUSD**: Fed vs. ECB — the largest and most liquid pair. Driven by US-EU rate differential, growth differential (GDP, PMI), and risk sentiment (EUR is a risk currency, USD is a safe haven).
- **GBPUSD**: Fed vs. BOE — GBP is the "wild card" — smaller, more volatile, sensitive to UK-specific political risk (Brexit legacy, fiscal credibility). Higher beta to risk sentiment than EURUSD.
- **USDJPY**: Fed vs. BOJ — the most asymmetric pair. BOJ is the only major central bank still running negative/zero rates (or very low). USDJPY is essentially a carry trade — it moves on the *divergence* between US and Japanese policy. BoJ intervention risk is unique.
- **AUDUSD**: Fed vs. RBA — AUD is a **commodity currency** (iron ore, coal, LNG exports to China). Driven by China growth, commodity prices, and RBA policy. AUDUSD often correlates with gold (both are risk-sensitive, both benefit from weaker USD).
- **USDCAD**: Fed vs. BOC — CAD is an **oil currency** (Canada is a major oil exporter). USDCAD correlates inversely with crude oil prices. Driven by oil fundamentals, BOC policy, and US-Canada trade dynamics.

**DXY (US Dollar Index)** is the master variable — all pairs are correlated to it. DXY itself is driven by the Fed's policy relative to the rest of the world.

### Current Macro Filter Mechanism

Same `compute_macro_regime()` pattern: 200-day SMA, EMA smoothing, `macro_label`/`macro_strength`. For EURUSD, this means: is EURUSD above or below its 200-day moving average? The SMA captures the *trend* but not the *cause*.

### Forecast Model Structure

| Quarter | Key Drivers | EURUSD Outlook | GBPUSD Outlook | USDJPY Outlook | AUDUSD Outlook | USDCAD Outlook |
|---------|------------|---------------|---------------|---------------|---------------|---------------|
| Q4 2026 | Fed/ECB divergence, DXY trajectory | | | | | |
| Q1 2027 | Rate path convergence/divergence, growth | | | | | |
| Q2 2027 | Inflation trajectory, labor market | | | | | |
| Q3 2027 | Cycle positioning, political risk | | | | | |

### Key Data Sources
- **Central Bank Calendars**: FOMC, ECB, BOJ, RBA, BOC decision dates
- **FRED**: Fed Funds Rate, ECB Deposit Rate, BOJ Policy Rate, US CPI, EU CPI, Japan CPI
- **TradingView**: EURUSD=X, GBPUSD=X, USDJPY=X, AUDUSD=X, USDCAD=X (via OANDA)
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

---

## Part 3: Equities

### Symbols
| symbol | tradingview_symbol | exchange | asset_class | price_precision |
|--------|-------------------|----------|-------------|-----------------|
| AAPL | AAPL | NASDAQ/Yahoo | stocks | 2 |
| AMZN | AMZN | NASDAQ/Yahoo | stocks | 2 |
| GOOGL | GOOGL | NASDAQ/Yahoo | stocks | 2 |
| META | META | NASDAQ/Yahoo | stocks | 2 |
| MSFT | MSFT | NASDAQ/Yahoo | stocks | 2 |
| NVDA | NVDA | NASDAQ/Yahoo | stocks | 2 |
| TSLA | TSLA | NASDAQ/Yahoo | stocks | 2 |
| SPY | SPY | NYSE/Yahoo | stocks | 2 |
| QQQ | QQQ | NASDAQ/Yahoo | stocks | 2 |

### Macro Driver: Earnings Cycle + Monetary Policy + Growth Expectations

Equities are driven by a fundamentally different mechanism than forex or commodities. The primary driver is **corporate earnings growth expectations** — discounted future cash flows. But the *discount rate* (determined by monetary policy) is equally important. This creates a **dual-engine** structure:

1. **Earnings engine**: Revenue growth, margin expansion, guidance revisions, sector rotation. Driven by the business cycle, consumer spending, enterprise IT investment, AI capex (currently the dominant theme for NVDA/MSFT/GOOGL).
2. **Discount rate engine**: Fed policy, real yields, risk premium. Higher rates compress P/E multiples, especially for growth stocks (long-duration assets). Lower rates expand multiples.

**Rate calendars matter differently for equities than for forex.** For forex, rate differentials drive the pair directly. For equities, the *level* of rates matters more than the *differential* — it's about the discount rate applied to future earnings. A 5% Fed rate is a headwind for equities regardless of what ECB does; a 2% Fed rate is a tailwind.

**Earnings season is the dominant catalyst.** Quarterly earnings reports (Jan/Apr/Jul/Oct) create predictable volatility patterns. Guidance revisions (not just actual results) drive price action. The current AI capex super-cycle means NVDA/MSFT/GOOGL earnings are market-moving events for SPY/QQQ.

**Sector rotation is a macro signal**: In late-cycle expansion, rotate from growth (NVDA/TSLA) to value (financials, energy). In recessions, rotate to defensives (utilities, staples — not in current symbol set). SPY and QQQ serve as beta benchmarks — they capture the broad market direction that individual stocks are measured against.

**NVDA is a special case**: It's simultaneously an equity (earnings-driven) and a macro indicator (AI capex cycle). NVDA's earnings can move the entire market (SPY, QQQ) — it has become a macro asset, not just a stock.

### Current Macro Filter Mechanism

Same `compute_macro_regime()` pattern. For AAPL, this means: is AAPL above or below its 200-day SMA? The 200-day SMA captures the long-term trend but doesn't distinguish between earnings-driven moves and rate-driven moves.

### Forecast Model Structure

| Quarter | Key Drivers | AAPL | NVDA | SPY | QQQ | TSLA |
|---------|------------|------|------|-----|-----|------|
| Q4 2026 | Earnings season, Fed path, AI capex | | | | | |
| Q1 2027 | GDP growth, margins, guidance | | | | | |
| Q2 2027 | Consumer spending, rates, cycle | | | | | |
| Q3 2027 | Political/regulatory risk, cycle | | | | | |

### Key Data Sources
- **Earnings calendars**: Company-specific earnings dates, consensus estimates (Zacks, Bloomberg)
- **FRED**: Fed Funds Rate, 10Y Treasury, GDP, CPI, Consumer Confidence, PMI
- **TradingView**: AAPL, AMZN, GOOGL, META, MSFT, NVDA, TSLA, SPY, QQQ (via Yahoo)
- **COT Report**: Speculative positioning in equity index futures (ES, NQ)
- **VIX**: Volatility index — risk-off signal for equities
- **Sector ETFs**: XLK, XLF, XLE, XLV (not in current symbol set but useful for rotation analysis)

### Existing `macro_filter.py` Integration
- Same pattern — `fetch_tradingview_batch()` fetches daily bars for all `stocks` asset_class symbols
- `annualise_factor = 252` for stocks (vs 8760 for crypto) in `_process_symbol()`
- `is_market_open()` check in orchestrator.py filters out stocks outside market hours
- SPY and QQQ serve as market benchmarks — their macro regime affects all individual stocks

### Gaps
- No earnings data integration (EPS, revenue, guidance — the dominant equity driver)
- No VIX or volatility data (critical for equity risk assessment)
- No sector rotation indicators
- No Fed "dot plot" or forward guidance parsing
- SPY/QQQ macro regime doesn't propagate to individual stocks automatically (each stock's SMA is independent)
- NVDA's macro-indicator status isn't captured by the current price-only SMA filter

---

## Part 4: Crypto

### Symbols
| symbol | tradingview_symbol | exchange | asset_class | price_precision |
|--------|-------------------|----------|-------------|-----------------|
| BTC | BTC | COINBASE/Hyperliquid | crypto | 1 |
| ETH | ETH | COINBASE/Hyperliquid | crypto | 2 |
| SOL | SOL | COINBASE/Hyperliquid | crypto | 4 |
| BTCUSD | COINBASE:BTCUSD | COINBASE/TradingView | crypto | 1 |
| ETHUSD | ETHUSD | COINBASE/TradingView | crypto | 2 |
| SOLUSD | SOLUSD | COINBASE/TradingView | crypto | 4 |

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

Same `compute_macro_regime()` pattern — 200-day SMA, EMA smoothing. For BTC, this means: is BTC above or below its 200-day moving average? The `annualise_factor = 8760` (24/7 market) vs 252 for stocks is the key difference in the Kelly sizing calculation.

Crypto trades 24/7/365 — no market hours filter. This means the macro regime is computed on continuous data without gaps, unlike equities which have weekend/holiday gaps.

### Forecast Model Structure

| Quarter | Key Drivers | BTC | ETH | SOL |
|---------|------------|-----|-----|-----|
| Q4 2026 | Fed liquidity, ETF flows, regulatory | | | |
| Q1 2027 | Adoption metrics, stablecoin supply | | | |
| Q2 2027 | Halving cycle positioning, on-chain | | | |
| Q3 2027 | Cycle positioning, regulatory clarity | | | |

### Key Data Sources
- **FRED**: Fed Funds Rate, M2 Money Supply (the most important crypto macro indicator), CPI
- **TradingView/Hyperliquid**: BTC, ETH, SOL (via Coinbase/Hyperliquid)
- **On-chain data**: Glassnode, Dune Analytics (active addresses, TVL, exchange flows, stablecoin supply)
- **Regulatory calendar**: SEC/CFTC decisions, ETF approval dates, legislative events
- **BTC Halving Schedule**: Next halving ~April 2028
- **CoinDesk/Cointelegraph**: Regulatory and adoption news

### Existing `macro_filter.py` Integration
- Same `compute_macro_regime()` pattern — `fetch_tradingview_batch()` fetches daily bars
- `annualise_factor = 8760` for crypto (24/7 market)
- `is_market_open()` returns True for crypto at all times (no market hours filter)
- `macro_regime_mode = "shadow"` for crypto symbols (same as all new symbols)
- BTC, ETH, SOL on Hyperliquid; BTCUSD, ETHUSD, SOLUSD on Coinbase via TradingView

### Gaps
- No M2 money supply data feed (the most important crypto macro indicator)
- No on-chain metrics integration (active addresses, TVL, exchange flows)
- No regulatory event calendar
- No BTC halving schedule tracking
- No BTC dominance tracking
- No stablecoin supply data
- No correlation analysis with SPY/NVDA (risk-on/risk-off regime detection)
- Crypto-specific volatility (80%+ annualized) makes the 200-day SMA filter less effective than for traditional assets

---

## Part 5: Implementation Requirements

### 5.1 New `ForecastSource` Entries for `macro_forecast_agent.py`

The existing `_COMMODITIES_SOURCE` pattern needs to be extended. The `FORECAST_SOURCES` tuple at line 84 currently contains only `_COMMODITIES_SOURCE`. Add three new sources:

```python
_PRECIOUS_METALS_SOURCE = ForecastSource(
    name="precious_metals_12m",
    doc_path=_DOCS_LLM_DIR / "precious_metals_12m_forecast_model_2027.md",
    symbols=("GOLD", "SILVER", "XAUUSD", "XAUEUR", "XAUAUD", "XAGUSD", "XAG"),
)

_FOREX_MAJORS_SOURCE = ForecastSource(
    name="forex_majors_12m",
    doc_path=_DOCS_LLM_DIR / "forex_majors_12m_forecast_model_2027.md",
    symbols=("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"),
)

_EQUITIES_SOURCE = ForecastSource(
    name="equities_12m",
    doc_path=_DOCS_LLM_DIR / "equities_12m_forecast_model_2027.md",
    symbols=("AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA", "SPY", "QQQ"),
)

_CRYPTO_SOURCE = ForecastSource(
    name="crypto_12m",
    doc_path=_DOCS_LLM_DIR / "crypto_12m_forecast_model_2027.md",
    symbols=("BTC", "ETH", "SOL", "BTCUSD", "ETHUSD", "SOLUSD"),
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

### 5.2 Four Forecast Model Documents to Create

| File | Symbols | Key Drivers | Size |
|------|---------|------------|------|
| `docs/LLM/precious_metals_12m_forecast_model_2027.md` | GOLD, SILVER, XAUUSD, XAUEUR, XAUAUD, XAGUSD, XAG | Real rates, USD, central bank buying, geopolitical risk | ~5KB |
| `docs/LLM/forex_majors_12m_forecast_model_2027.md` | EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD | Rate differentials, central bank divergence, DXY | ~5KB |
| `docs/LLM/equities_12m_forecast_model_2027.md` | AAPL, AMZN, GOOGL, META, MSFT, NVDA, TSLA, SPY, QQQ | Earnings cycle, discount rate, AI capex | ~8KB |
| `docs/LLM/crypto_12m_forecast_model_2027.md` | BTC, ETH, SOL, BTCUSD, ETHUSD, SOLUSD | Liquidity cycle, regulatory, halving, on-chain | ~5KB |

Each follows the same structure as `commodity_12m_forecast_model_2027.md` — quarterly outlook + named risk drivers per instrument.

### 5.3 System Prompt Adaptation in `macro_forecast_agent.py`

The `_SYSTEM_PROMPT` (line 95-114) is commodity-specific ("12-month forecast model", "quarterly outlook"). Needs to be generalized or have per-source system prompts. The `_build_prompt()` function (line 122) already accepts `doc_text` dynamically, so the prompt template is source-agnostic — just the `_SYSTEM_PROMPT` header needs to be flexible.

Options:
- **Option A**: One `_SYSTEM_PROMPT` that mentions all four asset classes and their driver framing
- **Option B**: Per-source `_SYSTEM_PROMPT` (commodity, metals, forex, equities, crypto) passed as a parameter to `classify_symbol()`
- **Option C**: The `_SYSTEM_PROMPT` already works if the `doc_text` contains the right framing — the LLM infers the asset class from the document content

**Option C is preferred** — the document itself provides the context, no code changes needed.

### 5.4 Supabase `nt_symbol` Entries Already Exist

All symbols are already registered in `nt_symbol` from previous migrations:
- Gold/silver: `XAUUSD` (OANDA), `XAGUSD` (OANDA) from migration 028; `GOLD`, `SILVER` from migration 001
- Forex: `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD` from migration 028 (OANDA via TradingView)
- Equities: `AAPL`, `AMZN`, `GOOGL`, `META`, `MSFT`, `NVDA`, `TSLA`, `SPY`, `QQQ` from migration 001 (Yahoo)
- Crypto: `BTC`, `ETH`, `SOL` from migration 001 (Hyperliquid); `BTCUSD`, `ETHUSD`, `SOLUSD` from migration 014 (Alpaca/Coinbase)

No new `nt_symbol` migrations needed — just update `asset_class` labels if needed (currently `precious_metals` for XAUUSD/XAGUSD, `forex` for EURUSD/etc., `stocks` for equities, `crypto` for BTC/ETH/SOL).

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
3. `run_macro_forecast()` successfully classifies all symbols across all 5 sources (12 commodities + 7 metals + 5 forex + 9 equities + 6 crypto = 39 symbols)
4. `nt_macro_forecast` table populated with classification rows for all new symbols
5. Shadow mode validation: compare LLM-based `current_macro_label` with `macro_filter.py`'s `macro_label` for divergence
6. Full pytest suite passes
7. No changes to `macro_filter.py`, `feature_flags.py`, `_PRICE_DECIMALS`, or `nt_symbol` schema

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
- **`_PRICE_DECIMALS`** (line 599-613): Definitive symbol list — all existing symbols covered
- **`annualise_factor = 8760 if asset_class == "crypto" else 252`** (line 2754-2755): Asset-class-specific annualization

### `feature_flags.py`
- **`macro_regime_mode`**: `shadow` — existing symbols already use this
- **`macro_forecast_agent_enabled`**: `True` — agent already enabled

---

*End of macro forecast scope for existing symbols.*
