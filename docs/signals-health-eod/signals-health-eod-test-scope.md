# Signals Health EOD — Single-Symbol Test Scope

> **Purpose:** End-to-end trial of the cronjob pipeline with a single symbol.
> **Status:** Ready to run (vars set, gateway running, prompt path fixed).

## Test Symbol

- **Symbol:** `AMZN` (Signal Scout plan)
- **Rationale:** Active symbol in `v_talaria_latest_signals` with `qualified = true`,
  belongs to the Signal Scout plan (`plan_ids` contains `scout`), routes to the
  `signal-scout-eod-report` webhook on channel `1544906978151891084`.
- **TradingView symbol:** `AMZN`

## Prerequisites (data setup)

The `v_talaria_latest_signals` view has **no `qualified = true` rows yet** for today.
Before running the test, insert one qualified signal for AMZN via the Supabase
REST API or directly into `nt_sweep_result`. The test query in the parent prompt
is:

```sql
SELECT s.symbol, s.signal, s.entry_price, s.stop_loss, s.take_profit,
       s.aggression, s.regime, s.sweep_timestamp,
       t.tradingview_symbol, t.plan_ids
FROM v_talaria_latest_signals s
JOIN v_talaria_symbols t ON t.symbol = s.symbol
WHERE s.qualified = true
  AND s.sweep_timestamp >= NOW()::date
  AND s.sweep_timestamp < NOW()::date + interval '1 day';
```

If this returns 0 rows, the prompt will find nothing to process. Insert a test
row into `nt_sweep_result` (the fastapi backend table) with:
- `symbol = 'AMZN'`
- `qualified = true`
- `signal = 'BUY'` (or `'SELL'`)
- `entry_price`, `stop_loss`, `take_profit` set to realistic values
- `sweep_timestamp = NOW()` (UTC)
- `regime = '🐂 Low-vol strong bull'` (or similar)
- `plan_ids` containing the Signal Scout plan UUID

Then confirm the view returns the row:
```sql
SELECT * FROM v_talaria_latest_signals WHERE symbol = 'AMZN' AND qualified = true;
```

## Test Steps

1. **Insert test data** — insert one qualified AMZN signal into `nt_sweep_result`
   with `sweep_timestamp = NOW()` (UTC) and `qualified = true`.
2. **Verify the view** — confirm `v_talaria_latest_signals` returns the AMZN row
   with `qualified = true` and `plan_ids` containing the Signal Scout UUID.
3. **Run the parent prompt one-shot** —
   `hermes chat -q "Read the EOD signal research prompt from
   /c/Users/aloys/OneDrive/Documents/GitHub/noble-trader-workspace/nobletradingapp/docs/signals-health-eod-prompt.md and run it." --oneshot`
   This triggers the delegate_task batch for AMZN.
4. **Subagent execution** — each subagent:
   - Web research (real-time) via `computer_use` + `browser_navigate`
   - TradingView chart read (real-time) via `computer_use`
   - Compute close-vs-signal delta
   - Post Discord embed via `--deliver discord --deliver-chat-id 1544906978151891084`
5. **Verify** — confirm the Discord embed arrives on channel `1544906978151891084`
   with all fields populated.

## Success Criteria

- One Discord embed posted to channel `1544906978151891084` for AMZN.
- Embed contains: symbol, signal, regime, entry/stop/target, calibration bias,
  web research headline, TV chart classification, close-vs-signal delta.
- Skip-and-continue works if research or TV read fails (partial embed posted).
- No secrets leaked in the Discord message.

## Notes

- The `SUPABASE_URL` + `SUPABASE_PUBLISHABLE_KEY` vars are already in
  `~/.hermes/profiles/astro/.env` and resolve correctly via `hermes chat`.
- The gateway is running (`hermes cron status` confirmed PID 18260).
- The prompt SQL was corrected to join `v_talaria_symbols` on `t.symbol = s.symbol`
  (the view has no `symbol_id` or `platform` columns — only `symbol`,
  `tradingview_symbol`, `plan_ids`).
- The per-symbol prompt lives at `~/.hermes/scripts/signals-health-eod-symbol.md`.
- The cron job `signals-health-eod` is scheduled at `0 19 * * *` with
  `--deliver discord:1544906978151891084` (confirmed via `hermes cron list`).
| This single test validates: per-symbol prompt, real-time research + TV chart,
  plan-scoped webhook routing, and Discord delivery.

## Run Log (2026-09-03)

**Cron job:** `signals-health-eod` (57e246190f0c), scheduled `0 19 * * *`, fired at `2026-09-03T13:27:19`. Delivery blocked_config (Discord not configured in astro profile).

**Manual delegate_task batch:** 4 subagents launched in a single call, all completed:

| Symbol | Signal | Web Research | TV Chart | Delta Computed | Embed Written | Calibration |
|---|---|---|---|---|---|---|
| AUDUSD | BUY | ⚠️ Skipped (Firecrawl 403) | ⚠️ Skipped (browser blocked) | ✅ -0.000425 | ✅ `2026-09-03-AUDUSD.json` | Not in table |
| US30USD | BUY | ✅ Done | ⚠️ Skipped (browser blocked) | ✅ -24.0 | ✅ `us30usd_buy_2026-09-03.json` | 0.0021 CALIBRATED |
| US500 | BUY | ✅ Done | ⚠️ Skipped (browser blocked) | ✅ -10.30 | ✅ `2026-09-03_US500_BUY_embed.json` | 0.319 OVERCONFIDENT |
| USDCHF | SELL | ✅ Done | ⚠️ Skipped (browser blocked) | ✅ -0.0005 | ✅ `usdchf_sell_2026-09-03.json` | 0.496 OVERCONFIDENT |

**Files created:**
- `docs/signals-health-eod-digests/2026-09-03.md` — Full EOD digest (parent aggregation)
- `docs/signals-health-eod-digests/2026-09-03-AUDUSD.json` — AUDUSD embed (1,953 bytes)
- `docs/signals-health-eod-digests/2026-09-03_US500_BUY_embed.json` — US500 embed (2,383 bytes)
- `docs/signals-health-eod-digests/us30usd_buy_2026-09-03.json` — US30USD digest (5,873 bytes)
- `docs/signals-health-eod-digests/usdchf_sell_2026-09-03.json` — USDCHF digest (5,618 bytes)

All JSON files validated via `json.tool`.

**Note on test symbol:** The test scope originally targeted AMZN (Signal Scout), but the actual `v_talaria_symbols` view contains forex/indices (AUDUSD, US30USD, US500, USDCHF, BTCUSD, XAU*, etc.) — no AMZN today. The test was run against the 4 actual qualified symbols that exist in production data.
