"""Backfill of rows lost/degraded by Supabase insert failures, 2026-09-23/24.

Dry-run by default (prints what would be written + the controls); --apply writes.

Every value written is copied from a surviving sibling row of the SAME tick or
from the orchestrator log line of that tick, and every source->target column
mapping was validated first on the 3,670 healthy ticks of the last 3 days
(backfill_validate.py: 100.0% equality for every column copied). Columns whose
source is not an exact copy stay NULL — nothing is estimated.

  1. nt_signal_sim   — 3 rows whose gate labels were stripped by the old
                       "retry without Kairos columns" fallback: gate_outcome /
                       failed_gates / suppress_reason re-derived from the log
                       verdict line (control: the same derivation on every
                       healthy labelled row since 08:30 must agree 100%).
                       The 5 Kairos factor stamps are NOT backfilled: a TDVA
                       re-fetch reproduces only 16/39 control rows exactly
                       (bar revisions / forming-bar timing) and spread_bps was a
                       live quote snapshot.
  2. nt_sweep_result — 6 rows lost to ReadTimeout: decision fields from the
                       nt_signal_sim row of the tick, markov/TA/regime_shift from
                       the nt_regime_log row of the tick, p_timesfm (+ aggression
                       where logged) from the log. Backtest KPIs, kelly_f,
                       size_mult, prev_regime, sweep_duration_ms stay NULL.
                       error = marker text so the rows are identifiable.
  3. nt_regime_log   — 1 AUDUSD row lost to HTTP 502: every field from the
                       nt_sweep_result row of the same tick.
"""
import importlib.util
import json
import re
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone

S = r"C:/Users/aloys/AppData/Local/hermes/profiles/astro/cache/scratch/cellpersist"
_argv = sys.argv
sys.argv = [sys.argv[0]]
spec = importlib.util.spec_from_file_location("sq", S + "/sq.py")
sq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sq)
sys.argv = _argv
APPLY = "--apply" in sys.argv

MARK = ("backfill 2026-09-24: original insert lost (ReadTimeout); rebuilt from same-tick "
        "nt_signal_sim + nt_regime_log + log p_timesfm; backtest KPIs/kelly_f/size_mult/"
        "prev_regime not recoverable (NULL)")


def ts(s: str) -> datetime:
    s = s.replace("+00", "").replace(",", ".")
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def q(sql):
    r = sq.q(sql)
    if isinstance(r, dict) and "message" in r:
        raise SystemExit("SQL ERROR: " + r["message"])
    return r


# ── 1. gate labels ────────────────────────────────────────────────────────
VERD = re.compile(r"^(\S+ \S+) (?:\[sweep:orch\] (\w+): (REJECTED — \d+ gate\(s\) failed: \[(.*?)\]|COOLDOWN SUPPRESSED — (.*)|DEDUP SUPPRESSED|QUALIFIED.*)"
                  r"|\[light:geometry\] (\w+) REJECT qualified)")
verdicts: dict[str, list[tuple[datetime, dict]]] = {}
for line in open(S + "/gate_lines.txt", encoding="utf-8", errors="replace"):
    m = VERD.match(line.strip())
    if not m:
        continue
    t = ts(m.group(1))
    if m.group(6):                      # geometry live reject overrides a QUALIFIED
        verdicts.setdefault(m.group(6), []).append((t, {"gate_outcome": "suppressed", "suppress_reason": "geometry_gate", "failed_gates": None}))
        continue
    sym, body = m.group(2), m.group(3)
    if body.startswith("REJECTED"):
        gates = sorted(g.strip().strip("'") for g in m.group(4).split(",") if g.strip())
        gates = [g for g in gates if g != "timesfm_available"]          # timesfm_gate_mode: soft
        lab = {"gate_outcome": "gate_fail", "suppress_reason": None, "failed_gates": ",".join(gates) or None}
    elif body.startswith("COOLDOWN"):
        lab = {"gate_outcome": "suppressed", "failed_gates": None,
               "suppress_reason": "cooldown_open" if "still OPEN" in m.group(5) else "cooldown_time"}
    elif body.startswith("DEDUP"):
        lab = {"gate_outcome": "suppressed", "suppress_reason": "content_dedup", "failed_gates": None}
    else:
        lab = {"gate_outcome": "qualified", "suppress_reason": None, "failed_gates": None}
    verdicts.setdefault(sym, []).append((t, lab))
for v in verdicts.values():
    v.sort(key=lambda x: x[0])


def derive(sym: str, t: datetime):
    v = verdicts.get(sym, [])
    i = bisect_right([x[0] for x in v], t + timedelta(seconds=1)) - 1
    if i < 0 or (t - v[i][0]).total_seconds() > 60:
        return None
    return v[i][1]


rows = q("""select signal_id, symbol, ts, gate_outcome, suppress_reason, failed_gates
            from nt_signal_sim where source='sweep-light' and ts >= '2026-09-24 08:30' order by ts""")
LOG_END = max(t for v in verdicts.values() for t, _ in v)
rows = [r for r in rows if ts(r["ts"]) <= LOG_END]      # control only where the pulled log covers
print(f"[labels] log verdicts cover up to {LOG_END.isoformat()}")
agree = disagree = nover = 0
bad = []
targets = []
for r in rows:
    d = derive(r["symbol"], ts(r["ts"]))
    if r["gate_outcome"] is None:
        targets.append((r, d))
        continue
    if d is None:
        nover += 1
        continue
    same = all((r[k] or None) == d[k] for k in ("gate_outcome", "suppress_reason", "failed_gates"))
    agree += same
    disagree += not same
    if not same and len(bad) < 6:
        bad.append({"db": {k: r[k] for k in ("symbol", "ts", "gate_outcome", "suppress_reason", "failed_gates")}, "log": d})
print(f"[labels] CONTROL healthy rows since 08:30: agree={agree} disagree={disagree} no_log_verdict={nover}")
for b in bad:
    print("   DISAGREE", json.dumps(b))
print(f"[labels] targets (gate_outcome NULL): {len(targets)}")
for r, d in targets:
    print("   ", r["symbol"], r["ts"], r["signal_id"], "->", d)

# ── 2. nt_sweep_result ────────────────────────────────────────────────────
LOST_SR = [  # symbol, ReadTimeout log time, p_timesfm (log), aggression (log, else None)
    ("US30USD", "2026-09-23 17:01:24.144", 0.9127, None),
    ("AUDUSD", "2026-09-23 19:00:26.841", 0.9500, "mid"),
    ("US30USD", "2026-09-23 19:01:32.588", 0.7571, None),
    ("AUDUSD", "2026-09-24 16:00:30.520", 0.0500, None),
    ("US500", "2026-09-24 16:01:46.390", 0.0525, "aggressive"),
    ("BTCUSD", "2026-09-24 18:45:31.136", 0.3065, None),
]


def sr_select(sym, err, p_tfm, aggr):
    t = ts(err) - timedelta(seconds=5)          # request start (old 5s client timeout)
    T = t.isoformat()
    aggr_sql = f"'{aggr}'" if aggr else "null"
    return f"""
    select s.symbol,
           (select asset_class from nt_sweep_result a where a.symbol=s.symbol and a.sweep_window='sweep-light'
             order by a.sweep_timestamp desc limit 1) asset_class,
           'sweep-light'::text sweep_window, s.source, '{T}'::timestamptz sweep_timestamp,
           s.signal, s.entry_price, s.stop_loss, s.take_profit, s.brick_size, s.sl_bricks, s.tp_bricks,
           s.p_win, s.ev, s.qualified, s.effective_kelly, s.regime, s.regime_conf, s.venue,
           g.markov_p_up, g.markov_p_dn, g.regime_shift, g.ta_adx, g.ta_ao, g.ta_bb_power, g.ta_macd, g.ta_rsi,
           {p_tfm}::float8 p_timesfm, {aggr_sql}::text aggression, '{MARK}'::text error,
           s.ts sim_ts, g.detected_at reg_ts, (g.regime = s.regime and g.regime_conf = s.regime_conf) consistent
    from nt_signal_sim s
    join lateral (select * from nt_regime_log g where g.symbol=s.symbol and g.source='sweep-light'
                  and g.detected_at between s.ts - interval '60 sec' and '{T}'::timestamptz + interval '5 sec'
                  order by g.detected_at desc limit 1) g on true
    where s.symbol='{sym}' and s.source='sweep-light'
      and s.ts between '{T}'::timestamptz - interval '60 sec' and '{T}'::timestamptz
      and not exists (select 1 from nt_sweep_result r where r.symbol='{sym}' and r.sweep_window='sweep-light'
                      and r.sweep_timestamp between '{T}'::timestamptz - interval '90 sec' and '{T}'::timestamptz + interval '90 sec')
    order by s.ts desc limit 1"""


SR_COLS = ("symbol, asset_class, sweep_window, source, sweep_timestamp, signal, entry_price, stop_loss, take_profit, "
           "brick_size, sl_bricks, tp_bricks, p_win, ev, qualified, effective_kelly, regime, regime_conf, venue, "
           "markov_p_up, markov_p_dn, regime_shift, ta_adx, ta_ao, ta_bb_power, ta_macd, ta_rsi, p_timesfm, aggression, error")

sr_plan = []
for sym, err, p, a in LOST_SR:
    got = q(sr_select(sym, err, p, a))
    sr_plan.append((sym, err, p, a, got))
    g = got[0] if got else None
    print(f"[sweep_result] {sym} {err}: " + (
        "SKIP (row exists or no same-tick source)" if not g else
        f"sim_ts={g['sim_ts']} reg_ts={g['reg_ts']} consistent={g['consistent']} regime={g['regime']} "
        f"signal={g['signal']} p_win={g['p_win']} q={g['qualified']} p_timesfm={g['p_timesfm']} aggr={g['aggression']}"))

# ── 3. nt_regime_log AUDUSD 21:45 ─────────────────────────────────────────
RL_T = "2026-09-24 21:45:13.6+00"
rl_sel = f"""
    select r.symbol, 'sweep-light'::text source, r.regime, r.regime_conf, r.markov_p_up, r.markov_p_dn,
           r.effective_kelly kelly_f, r.prev_regime, r.regime_shift, '{RL_T}'::timestamptz detected_at, r.venue,
           r.ta_adx, r.ta_ao, r.ta_bb_power, r.ta_macd, r.ta_rsi,
           r.sweep_timestamp sr_ts,
           (select g.regime from nt_regime_log g where g.symbol='AUDUSD' and g.detected_at < '{RL_T}' order by g.detected_at desc limit 1) prev_log_regime
    from nt_sweep_result r
    where r.symbol='AUDUSD' and r.sweep_window='sweep-light'
      and r.sweep_timestamp between '{RL_T}'::timestamptz and '{RL_T}'::timestamptz + interval '30 sec'
      and not exists (select 1 from nt_regime_log g where g.symbol='AUDUSD' and g.source='sweep-light'
                      and g.detected_at between '{RL_T}'::timestamptz - interval '60 sec' and '{RL_T}'::timestamptz + interval '60 sec')
    order by r.sweep_timestamp limit 1"""
rl = q(rl_sel)
print("[regime_log] AUDUSD 21:45:", rl if rl else "SKIP")
RL_COLS = ("symbol, source, regime, regime_conf, markov_p_up, markov_p_dn, kelly_f, prev_regime, regime_shift, "
           "detected_at, venue, ta_adx, ta_ao, ta_bb_power, ta_macd, ta_rsi")

# ── apply ─────────────────────────────────────────────────────────────────
if not APPLY:
    print("\nDRY RUN — re-run with --apply to write.")
    sys.exit(0)

if disagree or nover:
    raise SystemExit("label control not clean — refusing to apply")
out = {"labels": [], "sweep_result": [], "regime_log": []}
for r, d in targets:
    if d is None:
        continue
    fg = f"'{d['failed_gates']}'" if d["failed_gates"] else "null"
    sr_ = f"'{d['suppress_reason']}'" if d["suppress_reason"] else "null"
    out["labels"] += q(f"""update nt_signal_sim set gate_outcome='{d['gate_outcome']}', failed_gates={fg},
                           suppress_reason={sr_} where signal_id='{r['signal_id']}' and gate_outcome is null
                           returning signal_id, symbol, ts, gate_outcome, failed_gates, suppress_reason""")
for sym, err, p, a, got in sr_plan:
    if not got or not got[0]["consistent"]:
        continue
    out["sweep_result"] += q(f"insert into nt_sweep_result ({SR_COLS}) select {SR_COLS} from ({sr_select(sym, err, p, a)}) x "
                             f"returning id, symbol, sweep_timestamp")
if rl and rl[0]["prev_regime"] == rl[0]["prev_log_regime"]:
    out["regime_log"] += q(f"insert into nt_regime_log ({RL_COLS}) select {RL_COLS} from ({rl_sel}) x "
                           f"returning id, symbol, detected_at, regime")
print("\nAPPLIED:", json.dumps(out, default=str, indent=1))
json.dump(out, open(S + "/backfill_applied.json", "w"), default=str, indent=1)
