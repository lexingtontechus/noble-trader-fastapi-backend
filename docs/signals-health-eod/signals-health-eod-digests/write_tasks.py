import json, os

tasks = [
    {
        "goal": "EOD Signals Health — evaluate AUDUSD BUY signal for 2026-09-03. (1) Web research via real-time web search for 'AUDUSD forex news today catalysts' — get top catalyst headline + source + key numbers. Use web_search then web_extract on top result. (2) TradingView chart read for OANDA:AUDUSD — extract current price vs entry (0.720445), classify pattern (trending/ranging/breaking/stale). browser_exec blocked in autonomous cron mode — fall back to web_extract on https://www.tradingview.com/symbols/OANDA:AUDUSD/. (3) Compute close vs entry delta: entry=0.720445, stop=0.715075, take_profit=0.731185. (4) Write Discord embed JSON to docs/signals-health-eod-digests/2026-09-03-AUDUSD.json. Attempt hermes send --to discord:1544906978151891084 (skip if not configured). Symbol=AUDUSD, signal=BUY, entry=0.720445, stop=0.715075, target=0.731185, regime=med_low_vol_strong_bull, tv_symbol=OANDA:AUDUSD, plan=Signal Scout + Precision Pro, channel=1544906978151891084, calibration_bias=0.5234, OVERCONFIDENT, avg_predicted_p_win=0.5234, realized_win_rate=0.0. Skip-and-continue on failures. Working dir: nobletradingapp repo root.",
        "context": "Subagent in cron job EOD Signals Health 2026-09-03. Autonomous, no interactive user. Browser_exec/computer_use blocked (Chrome remote-debugging needs interactive approval). Use web_search + web_extract for real-time research and web_extract on TV symbol page for chart data. Discord may not be configured — write JSON to digests dir instead. Symbol: AUDUSD, Signal: BUY, Entry: 0.720445, Stop: 0.715075, Target: 0.731185, Regime: med_low_vol_strong_bull, TV symbol: OANDA:AUDUSD, Plan: Signal Scout + Precision Pro, Channel: 1544906978151891084, Calibration: bias=0.5234, OVERCONFIDENT, avg_p_win=0.5234, realized_win_rate=0.0. Write to docs/signals-health-eod-digests/2026-09-03-AUDUSD.json.",
        "output_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "signal": {"type": "string"},
                "status": {"type": "string"},
                "delta_computed": {"type": "string"},
                "discord_status": {"type": "string"},
                "json_file": {"type": "string"},
                "summary": {"type": "string"}
            },
            "required": ["symbol", "summary"]
        }
    },
    {
        "goal": "EOD Signals Health — evaluate BTCUSD SELL signal. (1) Web research for 'BTCUSD cryptocurrency news today catalysts'. (2) TradingView chart read for COINBASE:BTCUSD. (3) Compute close vs entry: entry=80927.345, stop=81667.073165, target=79743.779936. (4) Write Discord embed JSON to docs/signals-health-eod-digests/2026-09-04-BTCUSD.json. Symbol=BTCUSD, signal=SELL, entry=80927.345, stop=81667.07, target=79743.78, regime=med_high_vol_strong_bear, tv_symbol=COINBASE:BTCUSD, plan=Signal Scout + Precision Pro, channel=1544906978151891084, calibration=0.0955, CALIBRATED, avg_predicted_p_win=0.470, realized_win_rate=0.375. Skip-and-continue.",
        "context": "Subagent in cron job EOD Signals Health. Browser_exec blocked. Use web_search + web_extract. Discord may not be configured. Symbol: BTCUSD, Signal: SELL, Entry: 80927.345, Stop: 81667.07, Target: 79743.78, Regime: med_high_vol_strong_bear, TV symbol: COINBASE:BTCUSD, Plan: Signal Scout + Precision Pro, Channel: 1544906978151891084, Calibration: bias=0.0955, CALIBRATED, avg_p_win=0.470, realized=0.375. Write to docs/signals-health-eod-digests/2026-09-04-BTCUSD.json.",
        "output_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "signal": {"type": "string"},
                "status": {"type": "string"},
                "delta_computed": {"type": "string"},
                "discord_status": {"type": "string"},
                "json_file": {"type": "string"},
                "summary": {"type": "string"}
            },
            "required": ["symbol", "summary"]
        }
    },
    {
        "goal": "EOD Signals Health — evaluate US30USD BUY signal for 2026-09-03. (1) Web research for 'US30USD Dow Jones news today catalysts'. (2) TradingView chart read for OANDA:US30USD. (3) Compute close vs entry: entry=53692.5, stop=53393.862, target=54289.776. (4) Write Discord embed JSON to docs/signals-health-eod-digests/2026-09-03-US30USD.json. Symbol=US30USD, signal=BUY, entry=53692.5, stop=53393.862, target=54289.776, regime=med_low_vol_strong_bull, tv_symbol=OANDA:US30USD, plan=Signal Scout + Precision Pro, channel=1544906978151891084, calibration=0.0815, CALIBRATED, avg_predicted_p_win=0.476, realized_win_rate=0.395. Skip-and-continue.",
        "context": "Subagent in cron job EOD Signals Health 2026-09-03. Browser_exec blocked. Use web_search + web_extract. Discord may not be configured. Symbol: US30USD, Signal: BUY, Entry: 53692.5, Stop: 53393.862, Target: 54289.776, Regime: med_low_vol_strong_bull, TV symbol: OANDA:US30USD, Plan: Signal Scout + Precision Pro, Channel: 1544906978151891084, Calibration: bias=0.0815, CALIBRATED, avg_p_win=0.476, realized=0.395. Write to docs/signals-health-eod-digests/2026-09-03-US30USD.json.",
        "output_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "signal": {"type": "string"},
                "status": {"type": "string"},
                "delta_computed": {"type": "string"},
                "discord_status": {"type": "string"},
                "json_file": {"type": "string"},
                "summary": {"type": "string"}
            },
            "required": ["symbol", "summary"]
        }
    },
    {
        "goal": "EOD Signals Health — evaluate US500 BUY signal for 2026-09-03. (1) Web research for 'US500 S&P 500 news today catalysts'. (2) TradingView chart read for PEPPERSTONE:US500. (3) Compute close vs entry: entry=7667.775, stop=7581.85425, target=7954.1775. (4) Write Discord embed JSON to docs/signals-health-eod-digests/2026-09-03-US500.json. Symbol=US500, signal=BUY, entry=7667.775, stop=7581.85425, target=7954.1775, regime=low_vol_strong_bull, tv_symbol=PEPPERSTONE:US500, plan=Signal Scout + Precision Pro, channel=1544906978151891084, calibration=0.327, OVERCONFIDENT, avg_predicted_p_win=0.327, realized_win_rate=0.0. Skip-and-continue.",
        "context": "Subagent in cron job EOD Signals Health 2026-09-03. Browser_exec blocked. Use web_search + web_extract. Discord may not be configured. Symbol: US500, Signal: BUY, Entry: 7667.775, Stop: 7581.85425, Target: 7954.1775, Regime: low_vol_strong_bull, TV symbol: PEPPERSTONE:US500, Plan: Signal Scout + Precision Pro, Channel: 1544906978151891084, Calibration: bias=0.327, OVERCONFIDENT, avg_p_win=0.327, realized=0.0. Write to docs/signals-health-eod-digests/2026-09-03-US500.json.",
        "output_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "signal": {"type": "string"},
                "status": {"type": "string"},
                "delta_computed": {"type": "string"},
                "discord_status": {"type": "string"},
                "json_file": {"type": "string"},
                "summary": {"type": "string"}
            },
            "required": ["symbol", "summary"]
        }
    },
    {
        "goal": "EOD Signals Health — evaluate USDCHF BUY signal for 2026-09-03. (1) Web research for 'USDCHF Swiss Franc news today catalysts'. (2) TradingView chart read for OANDA:USDCHF. (3) Compute close vs entry: entry=0.81285, stop=0.806781, target=0.829034. (4) Write Discord embed JSON to docs/signals-health-eod-digests/2026-09-03-USDCHF.json. Symbol=USDCHF, signal=BUY, entry=0.81285, stop=0.806781, target=0.829034, regime=high_vol_strong_bull, tv_symbol=OANDA:USDCHF, plan=Signal Scout + Precision Pro, channel=1544906978151891084, calibration=0.7086, OVERCONFIDENT, avg_predicted_p_win=0.7086, realized_win_rate=0.0. Skip-and-continue.",
        "context": "Subagent in cron job EOD Signals Health 2026-09-03. Browser_exec blocked. Use web_search + web_extract. Discord may not be configured. Symbol: USDCHF, Signal: BUY, Entry: 0.81285, Stop: 0.806781, Target: 0.829034, Regime: high_vol_strong_bull, TV symbol: OANDA:USDCHF, Plan: Signal Scout + Precision Pro, Channel: 1544906978151891084, Calibration: bias=0.7086, OVERCONFIDENT, avg_p_win=0.7086, realized=0.0. Write to docs/signals-health-eod-digests/2026-09-03-USDCHF.json.",
        "output_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "signal": {"type": "string"},
                "status": {"type": "string"},
                "delta_computed": {"type": "string"},
                "discord_status": {"type": "string"},
                "json_file": {"type": "string"},
                "summary": {"type": "string"}
            },
            "required": ["symbol", "summary"]
        }
    }
]

out = os.path.join(os.path.dirname(__file__), 'tasks.json')
with open(out, 'w') as f:
    json.dump(tasks, f, indent=2)

print(f"Wrote {len(tasks)} tasks to {out}")
