"""Validate backfill column sources: on healthy ticks (last 3 days), how often
does each candidate source column equal the target column of the same
symbol x tick?  Only sources with >=99% equality are used in the backfill."""
import importlib.util
import json

spec = importlib.util.spec_from_file_location(
    "sq", r"C:/Users/aloys/AppData/Local/hermes/profiles/astro/cache/scratch/cellpersist/sq.py")
sq = importlib.util.module_from_spec(spec)
import sys  # noqa: E402

_argv = sys.argv
sys.argv = [sys.argv[0]]
spec.loader.exec_module(sq)
sys.argv = _argv

# sweep_result r  <- signal_sim s (same symbol, s.ts within 30s before r.sweep_timestamp)
SIM_COLS = ["signal", "entry_price", "stop_loss", "take_profit", "brick_size", "sl_bricks", "tp_bricks",
            "p_win", "ev", "qualified", "effective_kelly", "kelly_f", "regime", "regime_conf", "venue", "source"]
# sweep_result r  <- regime_log g (same symbol, g.detected_at within 30s before r.sweep_timestamp)
REG_COLS = ["markov_p_up", "markov_p_dn", "prev_regime", "regime_shift", "ta_adx", "ta_ao",
            "ta_bb_power", "ta_macd", "ta_rsi", "venue", "regime", "regime_conf"]

def pair_sql(src, key, cols, extra=""):
    eqs = ", ".join(f"round(100.0*avg((r.{c} is not distinct from x.{c})::int),1) {c}" for c in cols)
    return f"""
    select count(*) n, {eqs}
    from nt_sweep_result r
    join lateral (select * from {src} x where x.symbol=r.symbol {extra}
                  and x.{key} between r.sweep_timestamp - interval '30 sec' and r.sweep_timestamp
                  order by x.{key} desc limit 1) x on true
    where r.sweep_window='sweep-light' and r.sweep_timestamp > now() - interval '3 day'"""

out = {
    "sweep_result<-signal_sim": sq.q(pair_sql("nt_signal_sim", "ts", SIM_COLS, "and x.source='sweep-light'")),
    "sweep_result<-regime_log": sq.q(pair_sql("nt_regime_log", "detected_at", REG_COLS, "and x.source='sweep-light'")),
    # regime_log g  <- sweep_result r  (the reverse, for the lost AUDUSD regime row)
    "regime_log<-sweep_result": sq.q("""
    select count(*) n,
      round(100.0*avg((g.regime is not distinct from r.regime)::int),1) regime,
      round(100.0*avg((g.regime_conf is not distinct from r.regime_conf)::int),1) regime_conf,
      round(100.0*avg((g.markov_p_up is not distinct from r.markov_p_up)::int),1) markov_p_up,
      round(100.0*avg((g.markov_p_dn is not distinct from r.markov_p_dn)::int),1) markov_p_dn,
      round(100.0*avg((g.kelly_f is not distinct from r.effective_kelly)::int),1) kelly_f_eq_eff,
      round(100.0*avg((g.kelly_f is not distinct from r.kelly_f)::int),1) kelly_f_eq_kelly,
      round(100.0*avg((g.prev_regime is not distinct from r.prev_regime)::int),1) prev_regime,
      round(100.0*avg((g.regime_shift is not distinct from r.regime_shift)::int),1) regime_shift,
      round(100.0*avg((g.venue is not distinct from r.venue)::int),1) venue,
      round(100.0*avg((g.ta_rsi is not distinct from r.ta_rsi)::int),1) ta_rsi,
      round(100.0*avg((g.ta_adx is not distinct from r.ta_adx)::int),1) ta_adx,
      round(100.0*avg((g.ta_macd is not distinct from r.ta_macd)::int),1) ta_macd,
      round(100.0*avg((g.ta_ao is not distinct from r.ta_ao)::int),1) ta_ao,
      round(100.0*avg((g.ta_bb_power is not distinct from r.ta_bb_power)::int),1) ta_bb_power
    from nt_regime_log g
    join lateral (select * from nt_sweep_result r where r.symbol=g.symbol and r.sweep_window='sweep-light'
                  and r.sweep_timestamp between g.detected_at and g.detected_at + interval '30 sec'
                  order by r.sweep_timestamp limit 1) r on true
    where g.source='sweep-light' and g.detected_at > now() - interval '3 day'"""),
    "regime_log<-signal_sim(regime_conf,kelly)": sq.q("""
    select count(*) n,
      round(100.0*avg((g.regime_conf is not distinct from s.regime_conf)::int),1) regime_conf,
      round(100.0*avg((g.kelly_f is not distinct from s.effective_kelly)::int),1) kelly_eq_sim_eff,
      round(100.0*avg((g.kelly_f is not distinct from s.kelly_f)::int),1) kelly_eq_sim_kelly
    from nt_regime_log g
    join lateral (select * from nt_signal_sim s where s.symbol=g.symbol and s.source='sweep-light'
                  and s.ts between g.detected_at - interval '30 sec' and g.detected_at + interval '5 sec'
                  order by s.ts desc limit 1) s on true
    where g.source='sweep-light' and g.detected_at > now() - interval '3 day'"""),
}
print(json.dumps(out, indent=0, default=str))
