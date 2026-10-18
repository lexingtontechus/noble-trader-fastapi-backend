import json, os, sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter

# Use TMPDIR env var (set by Hermes in the terminal)
TMP = os.environ.get("TMPDIR", os.environ.get("_TMPDIR", ""))
NOW = datetime.now(timezone.utc)

def load(name):
    path = os.path.join(TMP, "pulse2024", name) if TMP else f"$TMPDIR/pulse2024/{name}"
    # If path doesn't exist, try Windows path
    if not os.path.exists(path):
        path = path.replace("/c/", "C:/").replace("/", "\\")
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"  ERROR loading {name}: {e}", file=sys.stderr)
        return []

# === 1. CALIBRATION ===
print('=== 1. CALIBRATION (nt_calibration_records, last 12h vs 24h) ===')
for label, fname in [('12h','calib12h.json'), ('24h','calib24h.json')]:
    rows = load(fname)
    n = len(rows)
    wins = sum(1 for x in rows if x.get('outcome') == 'win')
    losses = sum(1 for x in rows if x.get('outcome') == 'loss')
    preds = [x['predicted_p_win_raw'] for x in rows if x.get('predicted_p_win_raw') is not None]
    wr = wins/n if n else 0
    mp = sum(preds)/len(preds) if preds else 0
    gap = wr - mp
    briers = [x['calibration_brier'] for x in rows if x.get('calibration_brier') is not None]
    brier = sum(briers)/len(briers) if briers else 0
    print(f'  {label}: n={n} wins={wins} losses={losses} WR={wr:.1%} pred={mp:.1%} gap={gap:+.4f} brier={brier:.4f}')

# By symbol 24h
rows24 = load('calib24h.json')
if rows24:
    by_sym = defaultdict(lambda: {'wins':0,'n':0})
    for r in rows24:
        s = r.get('symbol', 'unknown')
        by_sym[s]['n'] += 1
        if r.get('outcome') == 'win':
            by_sym[s]['wins'] += 1
    print('  24h by symbol:')
    for s in sorted(by_sym.keys()):
        d = by_sym[s]
        wr_val = d['wins']/d['n'] if d['n'] else 0
        print(f'    {s}: n={d["n"]} WR={wr_val:.1%}')

# 12h deciles
rows12 = load('calib12h.json')
if rows12:
    def decile(val):
        if not isinstance(val, (int,float)): return 'unknown'
        idx = min(int(val * 10), 9)
        return idx + 1
    by_d = defaultdict(list)
    for r in rows12:
        d = decile(r.get('predicted_p_win_raw'))
        by_d[d].append(r.get('outcome') == 'win')
    print('  12h deciles:')
    for d in sorted(by_d.keys()):
        outcomes = by_d[d]
        wr_val = sum(outcomes)/len(outcomes) if outcomes else 0
        print(f'    decile {d}: n={len(outcomes)} WR={wr_val:.1%}')

# === 2. REACHABILITY ===
print('')
print('=== 2. REACHABILITY (nt_bracket_reachability) ===')
reach = load('reach2.json')
if reach:
    latest = reach[0].get('computed_at','?')
    print(f'  latest computed_at: {latest}')
    try:
        ts = datetime.fromisoformat(latest.replace('Z','+00:00'))
        age_min = (NOW - ts).total_seconds() / 60
        print(f'  age: {age_min:.1f} min')
    except: pass
    print(f'  cells: {len(reach)}')
    print('  sample cells:')
    for r in reach[:15]:
        print(f'    {r.get("symbol","?")}: bucket={r.get("distance_atr_bucket")} med={r.get("median_hours_to_resolve")}h p75={r.get("p75_hours")}h p90={r.get("p90_hours")}h expired={r.get("pct_expired")}')
else:
    print('  no data')

# === 3. LABEL FRESHNESS ===
print('')
print('=== 3. LABEL FRESHNESS ===')
macro = load('macro.json')
trend = load('trend.json')
for label, data in [('MACRO', macro), ('TREND', trend)]:
    if data:
        latest_ts = data[0].get('computed_at','')
        try:
            ts = datetime.fromisoformat(latest_ts.replace('Z','+00:00'))
            age_min = (NOW - ts).total_seconds() / 60
            print(f'  {label}: latest={latest_ts} (age={age_min:.1f}min)')
        except:
            print(f'  {label}: latest={latest_ts}')

# === 4. SIGNALS 12h ===
print('')
print('=== 4. SIGNALS 12h (nt_signal_sim) ===')
sigs = load('signals12h.json')
if sigs:
    n = len(sigs)
    q = sum(1 for x in sigs if x.get('qualified') == True)
    rt = sum(1 for x in sigs if x.get('strategy_archetype'))
    print(f'  total={n} qualified={q} routed={rt}')
    by_sym = Counter(x['symbol'] for x in sigs)
    print(f'  by symbol: {dict(sorted(by_sym.items(), key=lambda x: -x[1]))}')
    by_macro = Counter(x.get('macro_label','?') for x in sigs)
    by_trend = Counter(x.get('trend_label','?') for x in sigs)
    print(f'  macro_label: {dict(by_macro)}')
    print(f'  trend_label: {dict(by_trend)}')
    directional_macro = ['bull', 'bear']
    directional_trend = ['trending_up', 'trending_down']
    both = sum(1 for x in sigs
        if x.get('macro_label') in directional_macro
        and x.get('trend_label') in directional_trend)
    print(f'  both-directional: {both}/{n}')
    by_bucket = Counter(x.get('bracket_bucket','?') for x in sigs)
    print(f'  bracket_bucket: {dict(sorted(by_bucket.items()))}')
    by_arch = Counter(x.get('strategy_archetype','?') for x in sigs)
    print(f'  strategy_archetype: {dict(by_arch)}')
else:
    print('  no data')

# === 5. ACTIVE SYMBOLS ===
print('')
print('=== 5. ACTIVE SYMBOLS (asset_class) ===')
syms = load('symbols2.json')
if syms:
    by_ac = defaultdict(list)
    for r in syms:
        by_ac[r['asset_class']].append(r['symbol'])
    for ac in sorted(by_ac.keys()):
        print(f'  {ac}: {by_ac[ac]}')
else:
    syms = load('symbols.json')
    if syms:
        by_ac = defaultdict(list)
        for r in syms:
            if r.get('active'):
                by_ac[r['asset_class']].append(r['symbol'])
        for ac in sorted(by_ac.keys()):
            print(f'  {ac}: {by_ac[ac]}')

# === 6. SWEEP CADENCE ===
print('')
print('=== 6. SWEEP CADENCE (latest) ===')
sweeps = load('sweeps.json')
if sweeps:
    for s in sweeps[:10]:
        print(f'  {s.get("ts","?")} {s.get("symbol","?")} p_win={s.get("p_win","?")} qualified={s.get("qualified","?")}')
