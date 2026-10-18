# EOD Signals Health — Per-Symbol Subagent Prompt

**Channel:** `1544906978151891084` (plan-scoped per symbol)
**Delivery:** Hermes `--deliver discord --deliver-chat-id <channel>`

## Input

You receive the following symbol data:

- **Symbol:** {symbol}
- **Signal:** {signal} (BUY/SELL)
- **Entry:** {entry_price}, **Stop:** {stop_loss}, **Target:** {take_profit}
- **Regime:** {regime}
- **Today's calibration bias:** {calibration_bias}
- **Plan:** {plan} (Signal Scout / Precision Pro)
- **TradingView symbol:** {tradingview_symbol}
- **Channel:** {channel} (`1544906978151891084`)

## Tasks (in order)

### 1. Web research (real-time)

Search for "{symbol} stock news earnings today" and pull the top catalyst headline.
Use `computer_use` + `browser_navigate` to read live web pages — NOT a static proxy.
Record: top headline, source, and any key numbers (earnings, guidance, etc.).

### 2. TradingView chart (real-time)

Navigate to the TV widget embed URL for {tradingview_symbol}:
`s.tradingview.com/widgetembed/?symbol={tradingview_symbol}&interval=1D&theme=light`

Use `computer_use` to observe the chart. Read the current price structure
vs. the signal. Classify it: trending / ranging / breaking / stale.
Record: current price vs. entry, observed pattern, any notable levels.

### 3. Close-vs-signal delta

Compute today's close vs. entry price (signed delta).
If `v_eod_calibration_bias_latest` is available, include the calibration bias.

### 4. Post Discord embed

Post a single Discord message via `hermes --deliver discord --deliver-chat-id {channel}`
with a Discord embed containing:

- **Symbol:** {symbol}
- **Signal:** {signal} (colored chip — green for BUY, red for SELL)
- **Regime:** {regime}
- **Entry / Stop / Target:** {entry_price} / {stop_loss} / {take_profit}
- **Calibration bias:** {calibration_bias}
- **Web research:** top catalyst headline + source (1-2 lines)
- **TV chart:** current price vs. entry, observed pattern
- **Close-vs-signal:** signed delta
- **Plan:** {plan}

## Error handling

Skip-and-continue: if web research or TV read fails for this symbol,
skip that field and continue. Do NOT abort the whole subagent.
Post what you have with a "(partial)" note if research or TV failed.

## Constraints

- Do NOT read/write any secrets; use only the provided signal data
  and the anon Supabase key (`SUPABASE_PUBLISHABLE_KEY`) for any
  supplementary lookups.
- One web search + one TV chart read per symbol (single-query trial).
- Use the hermes Discord deliver adapter — no bot token or webhook
  URL hardcoded here. The adapter handles auth.
