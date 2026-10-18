# EOD Signals Health — Parent Cronjob Prompt

**Schedule:** `0 19 * * *` (19:00 UTC = 4:00 PM EDT)
**Channel:** `1544906978151891084` (plan-scoped per symbol)
**Runner:** `hermes chat -q` one-shot via `hermes cron create`

## Objective

Evaluate today's qualified trading signals against actual market performance.
Enrich each symbol with real-time web research and TradingView chart analysis.
Post a per-symbol Discord embed to the plan-specific channel.

## Execution Model

This prompt orchestrates a `delegate_task` batch — one subagent per symbol,
all launched in a single call. Each subagent runs the full per-symbol research
+ Discord post loop. The parent aggregates results.

## Step 1 — Fetch today's qualified signals

Query Supabase via the anon key (read-only SELECT):

```sql
SELECT s.symbol, s.signal, s.entry_price, s.stop_loss, s.take_profit,
       s.aggression, s.regime, s.sweep_timestamp,
       t.tradingview_symbol, t.plan_ids
FROM nt_sweep_result s
JOIN v_talaria_symbols t ON t.symbol = s.symbol
WHERE s.qualified = true
  AND s.sweep_timestamp >= NOW()::date
  AND s.sweep_timestamp < NOW()::date + interval '1 day'
ORDER BY s.symbol, s.sweep_timestamp DESC;
```

## Step 2 — Spawn delegate_task batch

One subagent per symbol, all launched in a single `delegate_task` call
(capped by `delegation.max_concurrent_children`, default 3). Each subagent
receives the symbol data plus the per-symbol prompt from
`~/.hermes/scripts/signals-health-eod-symbol.md`.

Each subagent determines the plan-specific Discord channel from the symbol's
`plan_ids`:
- Signal Scout → `--deliver discord --deliver-chat-id 1544906978151891084`
- Precision Pro → `--deliver discord --deliver-chat-id 1544906978151891084`

## Step 3 — Aggregate

Collect per-symbol results. Write a concise EOD digest summary
(`/c/Users/aloys/OneDrive/Documents/GitHub/noble-trader-workspace/nobletradingapp/docs/signals-health-eod-digests/YYYY-MM-DD.md`) — no new Supabase table.

## Notes

- Anon key is in the hermes profile `.env` (`SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`).
- Do NOT read/write secrets; use only the provided signal data and the anon key.
- Skip-and-continue: if research or TV read fails for a symbol, skip that field.
