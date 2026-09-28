# Existing-symbol 12-month macro forecast model (2026–2027)

Multi-asset companion to `commodity_12m_forecast_model_2027.md`.  
Covers every non-commodity `nt_symbol` row (forex, metals pairs, indices, equities, crypto).  
Used by `macro_forecast_agent.ForecastSource` — classification only; not a trading rule.

**Horizon:** 2026-Q4 → 2027-Q3. Update quarterly.

---

## A. FX (USD bloc)

### EURUSD
- **Q4 2026:** range 1.05–1.12. Drivers: ECB vs Fed cut path, energy base, Ukraine risk premium. Bias **neutral** with mild USD-soft tail if Fed cuts faster.
- **Q1 2027:** 1.04–1.14. Growth differential dominates; political risk (EU fiscal) is the main two-sided risk.
- **Q2–Q3 2027:** trend depends on real-rate gap; consolidating until US labor breaks.
- **Risks:** ECB hawkish surprise (EUR↑), US hard landing (EUR↓ then ↑ on safety flows).

### GBPUSD
- **Q4 2026:** 1.22–1.32. BoE easing slower than Fed supports GBP until UK growth stalls.
- **2027:** fiscal noise + services inflation keep ranges wide. Bias **neutral**.
- **Risks:** gilt stress (GBP↓), sticky UK wages (BoE hold, GBP↑).

### USDJPY
- **Q4 2026:** 140–158. BoJ normalization vs Fed cuts = two-sided; intervention tails.
- **2027:** gradual JPY strength if carry compresses; watch MoF.
- **Risks:** MoF intervention (JPY↑), US re-acceleration (JPY↓).

### USDCHF, USDCAD
- **CHF:** safe-haven + SNB; range 0.86–0.94 through Q1 2027. Bias **neutral**.
- **CAD:** oil-linked; USDCAD 1.32–1.42. Risk: crude collapse (CAD↓), US tariffs (CAD↓).

### AUDUSD
- **Q4 2026:** 0.62–0.70. China stimulus is the swing factor. Bias **neutral** with China-upside bull tail.
- **Risks:** China property (AUD↓), risk-on equity (AUD↑).

---

## B. Metals (XAU / XAG family)

### XAUUSD / XAUEUR / XAUAUD
- **Q4 2026:** gold supported by real-rate cuts + central-bank buying; XAUUSD 2,550–3,100 bias **bull** while real yields fall.
- **2027:** if inflation re-accelerates, range widens; treat as **bull** until Fed re-hikes.
- **Risks:** real-yield spike (XAU↓), crisis bid (XAU↑). XAUEUR/XAUAUD inherit USD/EUR/AUD FX noise.

### XAGUSD
- Higher beta than gold; industrial demand (China) + beta to gold. Bias **bull** in gold bull tape, **neutral** if industrial weak.

---

## C. Indices / equity CFDs

### US500, US30USD, UK100, RUSS2000
- **Q4 2026:** earnings + Fed path. US500 **neutral-to-bull** on soft landing; **bear** if growth scare.
- **2027:** valuation-sensitive; small caps (RUSS2000) more rate-sensitive than US500.
- **Risks:** AI capex disappointment (US30/US500↓), UK fiscal (UK100↓).

### Single stocks (AAPL, MSFT, META, NVDA, TSLA, INTC)
- Cluster to **AI capex + rates**. NVDA/MSFT/META **bull** while AI spend holds; TSLA **neutral** (idiosyncratic); INTC **bear** until foundry path clears; AAPL **neutral** (services hold, hardware flat).
- **Risks:** capex cut (AI complex↓), regulation (META/MSFT), EV price war (TSLA).

---

## D. Crypto

### BTCUSD
- **Q4 2026:** liquidity + ETF flows. Range 75k–120k; bias **bull** on M2 expansion, **neutral** if real yields rise.
- **2027:** cycle-dependent; high beta to risk assets. **Risks:** regulatory shock (BTC↓), liquidity squeeze (two-sided).

---

## Classification notes (for the agent)

- `current_macro_label`: bull / bear / neutral against **today’s** quarter slice above.
- `current_trend_label`: trending_up / trending_down / reversal_* / consolidating.
- `strength`: position toward range edge (1.0 = extreme).
- `thesis`: one falsifiable sentence. `drivers` / `risks`: structured arrays (rates, USD, China, AI capex, …).
- Do **not** invent prices outside these bands without stating it in `narrative`.
