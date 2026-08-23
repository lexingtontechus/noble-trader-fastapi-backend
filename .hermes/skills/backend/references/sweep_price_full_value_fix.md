# Sweep price precision — full-value fix (2026-08-05)

## Symptom
- Supabase `nt_sweep_result` stored **XAUUSD entry 4030.32 (2dp)** — user confirmed it
  should be **4030.xxx (3dp)** (OANDA quotes gold to 3dp). Other metals kept full
  precision (XAUEUR 3540.8175, XAUAUD 5775.7445, XAGUSD 57.5393, USDJPY 163.7225).
- Discord signal embed showed **AUDUSD SL `$0.70` == entry** (real SL 0.697), TP `$0.71`
  (real 0.706), and **XAUUSD TP `$432.16`** (should be `$4032.16` — leading digit lost).
- User's table name in the issue was "nt_sweep_log"; the real table is **`nt_sweep_result`**
  (user corrected mid-turn). `nt_sweep_log` is not a code symbol anywhere — search for
  `nt_sweep_result` when investigating.

## Root cause — THREE truncation classes in one pipeline
1. **Per-symbol decimals maps defaulted to 2dp for OANDA metals.**
   - Python: `_PRICE_DECIMALS` in `regime_platform/sweep_orchestrator/orchestrator.py`
     had `GOLD:2, SILVER:2` but NOT `XAUUSD`/`XAUEUR`/`XAUAUD`/`XAGUSD` → `_price_decimals()`
     returned default 2 → `round(price, decimals)` truncated.
   - TS: `PRICE_TICK_DECIMALS` in `supabase/functions/_shared/utils.ts` AND
     `supabase/functions/sweep/index.deployed.ts` (deployed bundle has its OWN copy —
     fix BOTH) lacked the metals → `DEFAULT_TICK_DECIMALS.commodities = 2` truncated via
     `roundPrice()` inside `computeTradeSignal`.
2. **Payload/save rounding used `decimals` (2dp) instead of full precision.**
   - `orchestrator.py` heartbeat payload + debug payload rounded entry/stop/tp to `decimals`.
   - `heavy_sweep.py` BOTH its Supabase save AND its Redis heartbeat payload rounded to
     `decimals`. (The light sweep's `log_sweep_result` already used `round(...,6)`.)
3. **Discord/Telegram embeds hardcoded `f"${price:.2f}"`** for Entry/Stop/Take → final 2dp
   truncation in the delivered card.

## Fix (all applied)
- `orchestrator.py`: `_PRICE_DECIMALS` += `XAUUSD:3, XAUEUR:4, XAUAUD:4, XAGUSD:4, XAU:3, XAG:4`.
  Heartbeat + debug payloads now `round(..., 6)` (full value), not `decimals`.
- `heavy_sweep.py`: Supabase save + Redis payload both `round(..., 6)`.
- `notifications/discord.py`: added `_fmt_price(value)` / `_fmt_price_usd(value)` —
  format `f"{v:.6f}"` → rstrip trailing zeros → keep ≥2dp. Used for Entry/Stop/Take +
  description + execution Price/Stop-Loss/Take-Profit.
- `notifications/telegram.py`: same `_fmt_price_usd` helper for Entry/SL/TP lines.
- TS: `PRICE_TICK_DECIMALS` in BOTH `_shared/utils.ts` and `index.deployed.ts` +=
  `XAUUSD:3, XAUEUR:4, XAUAUD:4, XAGUSD:4, XAU:3, XAG:4`.

## Formatter behavior (verified)
```
_fmt_price_usd(4030.325) -> "$4030.325"   # was "$4030.32"
_fmt_price_usd(0.697)    -> "$0.697"      # was "$0.70" (== entry)
_fmt_price_usd(0.706)    -> "$0.706"      # was "$0.71"
_fmt_price_usd(4032.16)  -> "$4032.16"    # was "$432.16"
_fmt_price_usd(0.70)     -> "$0.70"       # keeps 2dp floor
_fmt_price_usd(None)     -> ""
```

## Verification
- New regression tests: `tests/test_price_full_value.py` — **11 passed** (formatter cases,
  decimals map, `_compute_trade_signal` full precision, 6dp heartbeat rounding).
- Full suite: `PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q
  --ignore=tests/test_renko_backtest.py` → **21 failed, 191 passed, 3 errors**.
  IMPORTANT: the 21 failures + 3 errors are **PRE-EXISTING and unrelated** — proven by
  `git stash` + re-run on clean HEAD (identical 21/180/3 before the new tests). They live
  in `test_backtest_cache.py`, `test_data_quality.py`, `test_execution_modeling.py`
  (JWT_SECRET_KEY env / Phase7 params / market-impact). `test_renko_backtest.py` has a
  pre-existing collection error (`RenkoBacktestRequest` not exported from `renko.router`).
  Do NOT attribute these to sweep/notification work; run the suite with `--ignore` and
  compare against clean HEAD if in doubt.
- `pytest` module was NOT in the repo `.venv` — install with
  `uv pip install pytest pytest-asyncio` (NOT bare `pip`, which resolves to the broken
  global hermes-agent venv).

## Repo-path + tooling gotchas hit this session
- The user's first path `...\GitHub\noble-trader-fastapi-backend` (NO
  `noble-trader-workspace` component) is WRONG — canonical is
  `C:\Users\aloys\OneDrive\Documents\GitHub\noble-trader-workspace\noble-trader-fastapi-backend`.
- `search_files` fails on this repo's root with `IO error: system cannot find the path
  specified` for BOTH `C:\...` and `C:/...` forms (OneDrive reparse quirk, same family as
  `nobletradingapp`). Use `terminal` `grep -rln` from the repo root.
- `read_file` reports `price_fetcher.py` as "binary" (CRLF) — read via terminal
  `python3 -c "open(...).read()"` or `sed -n`.
