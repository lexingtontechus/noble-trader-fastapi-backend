"""monitors.py — decay + rolling-window monitors (CRT-1).

Port of the EA's ``ComputeMonitors`` and ``ComputeRolling``:

- the CUSUM state machine itself imports ``regime_edge_crt.decay.
  DecayMonitor`` — the ONE source of truth for the decay rules (CRT-0 landed
  it; the optimizer's replay and the live engine share it, never a second
  port);
- the monitor REPLAY is stateless: given the option's paper labels (every
  raw setup of the option, resolved, c3-time AFTER the option's train_end,
  in c2-time order — the EA's record order), it rebuilds CUSUM state, live
  EV/n, and the decay roll mean exactly like the EA recomputes them on every
  bar close;
- the decay filter honors the README honesty note: only setups after
  train_end count ("the decay monitor must only count setups after the train
  end, exactly as the EA does");
- the v5 rolling window (45d wall-clock at compute time) does NOT filter on
  train_end — the README's own note: "the rolling window overlaps the
  training data. It's a live health check, not a validation." Counts are
  UNFILTERED paper labels (the EA's rlN/rlEV path; only the dashboard-only
  R-grid was filtered, and that surface is not ported);
- the TF pool aggregates the ENABLED options' records of one (symbol, TF) —
  the OPTION_THEN_TF fallback basis.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import numpy as np

from crt.config_loader import OptionCfg
from crt.labeler import LabelResult
from regime_edge_crt.decay import NORMAL, OFF, DecayMonitor


@dataclass
class LabelRow:
    """One paper-labeled setup (the monitor stream's atom)."""

    setup: Any                    # crt.setup.SetupRecord
    option: OptionCfg
    label: LabelResult
    c3_time: float                # epoch seconds — the setup's C3 boundary


def _decay_namespace(decay_cfg: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**{k: decay_cfg[k] for k in (
        "kappa_min", "kappa_frac", "h_caution", "h_off",
        "roll_n", "roll_off_ev", "react_n")})


def replay_monitor(option: OptionCfg, rows: list[LabelRow],
                   decay_cfg: dict[str, Any]) -> dict[str, Any]:
    """Rebuild one option's monitor state from its label stream.

    ``rows`` must be c2-time ascending and pre-filtered to this option.
    Mirrors the EA: labels after train_end only; mu0/sd0 from the option's
    OOS baseline; final state = DISABLED -> OFF, monitor-off -> NORMAL.
    Returns {state, s_cusum, live_n, live_ev, roll_ev}.
    """
    mon = DecayMonitor(option.base_ev, option.base_sd, _decay_namespace(decay_cfg))
    train_end = option.train_end.timestamp() if option.train_end else None
    n, total = 0, 0.0
    for row in rows:
        if not row.label.closed:
            continue
        if train_end is not None and row.c3_time <= train_end:
            continue    # README honesty note: after train_end only
        mon.update(row.label.r)
        n += 1
        total += row.label.r
    if option.enabled and decay_cfg.get("use_monitor", True):
        state = mon.state
    elif option.enabled:
        state = NORMAL
    else:
        state = OFF
    hist = mon.hist or []
    roll_ev = float(np.mean(hist[-int(decay_cfg["roll_n"]):])) if len(hist) else 0.0
    return {
        "state": state,
        "s_cusum": float(mon.S),
        "live_n": int(n),
        "live_ev": (total / n) if n else 0.0,
        "roll_ev": roll_ev,
    }


def apply_monitor(option: OptionCfg, result: dict[str, Any]) -> None:
    """Write a replay result back onto the OptionCfg (EA g_cfg[c] fields)."""
    option.state = int(result["state"])
    option.s_cusum = float(result["s_cusum"])
    option.live_n = int(result["live_n"])
    option.live_ev = float(result["live_ev"])
    option.roll_ev = float(result["roll_ev"])


def compute_rolling(rows: list[LabelRow], now_epoch: float,
                    roll_days: int) -> tuple[dict[tuple[str, int], tuple[int, float]], tuple[int, float]]:
    """EA ComputeRolling: per-option and pooled-TF rolling stats.

    Window: label c3_time >= now - roll_days*86400 (wall clock, no
    train_end filter — the README's overlap honesty note). Returns
    ({option_key: (rl_n, rl_ev)}, (tf_n, tf_ev)) with rl_ev = mean R.
    The TF pool counts the ENABLED options' labels only (EA ``if enabled``).
    """
    horizon = now_epoch - roll_days * 86400.0
    per: dict[tuple[str, int], list[float]] = {}
    pool: list[float] = []
    for row in rows:
        if row.c3_time < horizon:
            continue
        if not row.label.closed:
            continue
        per.setdefault(row.option.key, []).append(row.label.r)
        if row.option.enabled:
            pool.append(row.label.r)
    per_out = {k: (len(v), (sum(v) / len(v))) for k, v in per.items()}
    tf_out = (len(pool), (sum(pool) / len(pool)) if pool else 0.0)
    return per_out, tf_out


def apply_rolling(options: dict[tuple[str, int], OptionCfg],
                  per: dict[tuple[str, int], tuple[int, float]]) -> None:
    """Write rolling stats back onto the options (EA g_cfg[c].rlN/rlEV)."""
    for key, opt in options.items():
        n, ev = per.get(key, (0, 0.0))
        opt.rl_n, opt.rl_ev = int(n), float(ev)
