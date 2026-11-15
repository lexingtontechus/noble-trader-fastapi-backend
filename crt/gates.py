"""gates.py — the option gate chain, frozen at C2 close (CRT-1).

Port of the EA's ``SelectTargetOpt`` / ``CfgFilterPass`` / ``RollingPass`` /
``OptRiskPercent``. The gate order is VERBATIM (first failure wins):

    1. option disabled by the optimizer        -> fail
    2. option filters (sweep/chop/vol regime)  -> fail ("Filtered: …")
    3. decay monitor OFF                       -> fail ("Edge decay: OFF")
    4. rolling-window EV gate                  -> fail (reason text)
    5. else pass (CAUTION noted in the reason)

One decision per setup, taken at C2 close (arm time) and kept through C3 —
no flicker. ``risk_pct`` is frozen WITH the decision (confidence sizing at
the monitor state as of C2 close, CAUTION multiplier included).

Rolling gate (v5, ``ROLL_OPTION_THEN_TF`` default): the option's rolling
paper EV over the last ``roll_days`` needs >= ``roll_min_n`` resolved trades
AND EV > ``roll_min_ev``; a thin option falls back to the pooled value of
all ENABLED options on the same (symbol, TF); still thin -> blocked with the
sample-size reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crt.config_loader import OptionCfg
from crt.setup import SetupRecord

NORMAL, CAUTION, OFF = 0, 1, 2


@dataclass(frozen=True)
class ChopThresholds:
    """EA chop inputs (CfgFilterPass reads the EA's global inputs)."""

    max_wick_ratio: float = 0.70
    max_overlap_ratio: float = 0.75
    max_direction_changes: int = 4

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "ChopThresholds":
        cfg = cfg or {}
        return cls(
            max_wick_ratio=float(cfg.get("max_wick_ratio", 0.70)),
            max_overlap_ratio=float(cfg.get("max_overlap_ratio", 0.75)),
            max_direction_changes=int(cfg.get("max_direction_changes", 4)),
        )


@dataclass(frozen=True)
class SizingCfg:
    """EA confidence-sizing + risk-cap inputs (paper basis in crt_engine.yaml)."""

    use_confidence_sizing: bool = True
    base_risk_percent: float = 0.25
    max_risk_percent: float = 0.50
    conf_t_low: float = 1.0
    conf_t_high: float = 3.0
    caution_mult: float = 0.5
    paper_equity: float = 100000.0
    max_open_risk_percent: float = 1.0
    daily_loss_stop_percent: float = 2.0
    one_position_per_symbol: bool = True

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "SizingCfg":
        cfg = cfg or {}
        return cls(
            use_confidence_sizing=bool(cfg.get("use_confidence_sizing", True)),
            base_risk_percent=float(cfg.get("base_risk_percent", 0.25)),
            max_risk_percent=float(cfg.get("max_risk_percent", 0.50)),
            conf_t_low=float(cfg.get("conf_t_low", 1.0)),
            conf_t_high=float(cfg.get("conf_t_high", 3.0)),
            caution_mult=float(cfg.get("caution_mult", 0.5)),
            paper_equity=float(cfg.get("paper_equity", 100000.0)),
            max_open_risk_percent=float(cfg.get("max_open_risk_percent", 1.0)),
            daily_loss_stop_percent=float(cfg.get("daily_loss_stop_percent", 2.0)),
            one_position_per_symbol=bool(cfg.get("one_position_per_symbol", True)),
        )


@dataclass(frozen=True)
class RollingCfg:
    """EA v5 rolling-window gate inputs."""

    use_gate: bool = True
    roll_days: int = 45
    roll_min_n: int = 15
    roll_min_ev: float = 0.0
    scope: str = "option_then_tf"   # option | tf | option_then_tf (EA enum)

    @classmethod
    def from_cfg(cls, cfg: dict | None) -> "RollingCfg":
        cfg = cfg or {}
        return cls(
            use_gate=bool(cfg.get("use_gate", True)),
            roll_days=int(cfg.get("roll_days", 45)),
            roll_min_n=int(cfg.get("roll_min_n", 15)),
            roll_min_ev=float(cfg.get("roll_min_ev", 0.0)),
            scope=str(cfg.get("scope", "option_then_tf")).lower(),
        )


@dataclass
class GateDecision:
    """The frozen per-setup verdict (EA g_frzSetup/g_frzPass/g_frzWhy/g_frzRisk)."""

    passed: bool
    reason: str
    risk_pct: float
    frozen_at: float          # epoch seconds — the C2 close
    option_key: tuple[str, int]
    # CRT-2 provenance: the monitor snapshot AS CONSUMED at freeze time
    # (state/s_cusum/live_n/live_ev/roll_ev/rl_n/rl_ev) — stamped by the
    # engine right after freeze_decision, read by the claims builder so
    # the claim records the decision INPUTS, not a re-derivation. Never
    # read by any gate rule.
    monitor: dict | None = None


def vol_bit(p: float, vol_low: float = 0.33, vol_high: float = 0.67) -> int:
    """EA VolBit: p<0 unknown->mid(2); <low->1; <high->2; else 4."""
    if p < 0:
        return 2
    if p < vol_low:
        return 1
    if p < vol_high:
        return 2
    return 4


def cfg_filter_pass(setup: SetupRecord, option: OptionCfg,
                    chop: ChopThresholds, vol_low: float = 0.33,
                    vol_high: float = 0.67) -> tuple[bool, str]:
    """EA CfgFilterPass: sweep cap, chop, vol-regime mask (in that order)."""
    if option.max_sweep > 0 and setup.sweep_atr > option.max_sweep:
        return False, f"sweep {setup.sweep_atr:.2f} > {option.max_sweep:.2f} ATR"
    if option.chop == 1:
        if not (setup.chop_wick <= chop.max_wick_ratio
                and setup.chop_overlap <= chop.max_overlap_ratio
                and setup.dir_changes <= chop.max_direction_changes):
            return False, "chop"
    if option.vol_mask != 7:
        if (vol_bit(setup.vol_pct, vol_low, vol_high) & option.vol_mask) == 0:
            return False, "vol regime"
    return True, ""


def opt_risk_percent(option: OptionCfg, monitor_state: int,
                     sizing: SizingCfg) -> float:
    """EA OptRiskPercent: confidence ladder on OOS t, CAUTION multiplier."""
    pct = sizing.base_risk_percent
    if sizing.use_confidence_sizing:
        conf = 0.0
        if sizing.conf_t_high > sizing.conf_t_low:
            conf = max(0.0, min(1.0, (option.oos_t - sizing.conf_t_low)
                                / (sizing.conf_t_high - sizing.conf_t_low)))
        pct = sizing.base_risk_percent + (sizing.max_risk_percent - sizing.base_risk_percent) * conf
    if monitor_state == CAUTION:
        pct *= sizing.caution_mult
    return pct


def rolling_pass(option: OptionCfg, tf_roll: tuple[int, float],
                  roll: RollingCfg) -> tuple[bool, str]:
    """EA RollingPass (ROLL_OPTION_THEN_TF): option first, pooled TF fallback.

    ``tf_roll`` is (n, ev) pooled over the ENABLED options of this (symbol, TF).
    """
    if not roll.use_gate:
        return True, ""
    tf_n, tf_ev = tf_roll
    if roll.scope != "tf" and option.rl_n >= roll.roll_min_n:
        if option.rl_ev > roll.roll_min_ev:
            return True, ""
        return False, (f"Rolling {roll.roll_days}d EV {option.rl_ev:+.2f}R "
                       f"(n{option.rl_n}) not > {roll.roll_min_ev:.2f}")
    if roll.scope != "option" and tf_n >= roll.roll_min_n:
        if tf_ev > roll.roll_min_ev:
            return True, ""
        return False, (f"Rolling {roll.roll_days}d TF EV {tf_ev:+.2f}R "
                       f"(n{tf_n}) not > {roll.roll_min_ev:.2f}")
    n_shown = tf_n if roll.scope == "tf" else option.rl_n
    return False, (f"Rolling {roll.roll_days}d sample too small "
                   f"(n{n_shown} < {roll.roll_min_n})")


def freeze_decision(setup: SetupRecord, option: OptionCfg,
                    monitor_state: int, tf_roll: tuple[int, float],
                    chop: ChopThresholds, sizing: SizingCfg,
                    roll: RollingCfg, use_decay_monitor: bool = True,
                    now_epoch: float = 0.0) -> GateDecision:
    """The full EA SelectTargetOpt chain, evaluated ONCE at C2 close."""
    risk_pct = opt_risk_percent(option, monitor_state, sizing)
    passed, reason = False, ""
    if not option.enabled:
        reason = "Option disabled by optimizer"
    else:
        ok, why = cfg_filter_pass(setup, option, chop)
        if not ok:
            reason = f"Filtered: {why}"
        elif use_decay_monitor and monitor_state == OFF:
            reason = "Edge decay: OFF"
        else:
            ok, why = rolling_pass(option, tf_roll, roll)
            if not ok:
                reason = why
            else:
                passed = True
                reason = ("Edge CAUTION (reduced size)" if monitor_state == CAUTION
                          else "Edge NORMAL, rolling EV > 0")
    return GateDecision(passed=passed, reason=reason, risk_pct=risk_pct,
                        frozen_at=now_epoch, option_key=option.key)


def execution_caps(risk_state: dict[str, Any], symbol: str,
                   sizing: SizingCfg) -> tuple[bool, str]:
    """EA ExecutionAllowed risk caps (paper basis).

    ``risk_state`` carries: open_risk_cash, day_pnl, open_symbols (set).
    Order mirrors the EA: daily loss stop, then max open risk, then
    one-position-per-symbol.
    """
    if sizing.daily_loss_stop_percent > 0:
        day_loss_pct = -(float(risk_state.get("day_pnl") or 0.0)) / sizing.paper_equity * 100.0
        if day_loss_pct >= sizing.daily_loss_stop_percent:
            return False, "Daily loss stop"
    if sizing.max_open_risk_percent > 0:
        room = sizing.paper_equity * sizing.max_open_risk_percent / 100.0 \
            - float(risk_state.get("open_risk_cash") or 0.0)
        if room <= 0:
            return False, "Max open risk reached"
    if sizing.one_position_per_symbol and symbol in (risk_state.get("open_symbols") or set()):
        return False, "Managed order already open"
    return True, "READY"
