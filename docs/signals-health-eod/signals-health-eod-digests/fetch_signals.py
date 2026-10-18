import json, os, requests

url = 'https://pcvscowltlrxzgxjurcr.supabase.co'
key = 'sb_publishable_cYfseJa9z0qss0g_Y594wA_lXrWVBsa'

headers = {
    'apikey': key,
    'Authorization': 'Bearer ' + key,
    'Accept': 'application/json',
}

# Fetch today's qualified signals — Sep 3 UTC (the PDT trading day)
# The parent prompt uses NOW()::date which in UTC is Sep 4 at this moment (7:05 PM PDT = 00:05 Sep 4 UTC)
# But the previous run used Sep 3 data, so we filter for Sep 3 UTC date range
sig_res = requests.get(
    f'{url}/rest/v1/nt_sweep_result',
    headers=headers,
    params={
        'qualified': 'eq.true',
        'select': 'symbol,signal,entry_price,stop_loss,take_profit,aggression,regime,sweep_timestamp',
        'sweep_timestamp': 'gte.2026-09-03T00:00:00Z',
        'sweep_timestamp': 'lt.2026-09-04T00:00:00Z',
        'order': 'sweep_timestamp.desc',
    },
    timeout=30,
)

# Fetch v_talaria_symbols (metadata with tradingview_symbol + plan_ids)
sym_res = requests.get(f'{url}/rest/v1/v_talaria_symbols', headers=headers, timeout=30)

# Fetch v_eod_calibration_bias_latest
cal_res = requests.get(f'{url}/rest/v1/v_eod_calibration_bias_latest', headers=headers, timeout=30)

# Also fetch v_eod_calibration_bias (not just "latest" — get all)
cal_all_res = requests.get(f'{url}/rest/v1/v_eod_calibration_bias', headers=headers, timeout=30)

signals = sig_res.json()
symbols_meta = sym_res.json()
cal_data = cal_res.json()
cal_all = cal_all_res.json() if cal_all_res.status_code == 200 else []

# Build symbol metadata lookup
sym_lookup = {s['symbol']: s for s in symbols_meta}

# Build calibration lookup: try latest first, then historical for Sep 3
cal_lookup = {}
for c in cal_data:
    if c.get('day') == '2026-09-04':
        cal_lookup[c['symbol']] = c

# If no Sep 4 data, try Sep 3
if not cal_lookup:
    for c in cal_all:
        if c.get('day') == '2026-09-03':
            cal_lookup[c['symbol']] = c

# Map plan_ids to plan names
plan_map = {
    'df980ef1-e41f-41db-9d04-2ad09da69626': 'Signal Scout',
    '1b66e78e-e8d1-46b6-9887-b36e038131c5': 'Precision Pro',
}

CHANNEL_ID = 1544906978151891084

# Get latest signal per symbol (ordered desc, so first occurrence is latest)
latest_signals = {}
for row in signals:
    sym = row['symbol']
    if sym not in latest_signals:
        latest_signals[sym] = row

# Enrich with metadata + calibration
enriched = []
for sym in sorted(latest_signals.keys()):
    sig = latest_signals[sym]
    meta = sym_lookup.get(sym, {})
    cal = cal_lookup.get(sym, {})
    plan_names = [plan_map.get(pid, pid) for pid in meta.get('plan_ids', [])]
    combined = {
        'symbol': sig['symbol'],
        'signal': sig['signal'].upper(),
        'entry_price': sig['entry_price'],
        'stop_loss': sig['stop_loss'],
        'take_profit': sig['take_profit'],
        'aggression': sig['aggression'],
        'regime': sig['regime'],
        'sweep_timestamp': sig['sweep_timestamp'],
        'tradingview_symbol': meta.get('tradingview_symbol', sym),
        'exchange': meta.get('exchange', ''),
        'plan_ids': meta.get('plan_ids', []),
        'plan': ' + '.join(plan_names) if plan_names else 'Unknown',
        'channel': CHANNEL_ID,
        'calibration_bias': cal.get('bias'),
        'calibration_status': cal.get('status'),
        'avg_predicted_p_win': cal.get('avg_predicted_p_win'),
        'realized_win_rate': cal.get('realized_win_rate'),
    }
    enriched.append(combined)

# Save to file
out = os.path.join(os.getcwd(), 'today_signals.json')
with open(out, 'w') as f:
    json.dump(enriched, f, indent=2)

print(f'Symbols found: {len(enriched)}')
print(json.dumps(enriched, indent=2))
