"""engine.py — the per-closed-bar CRT engine (CRT-1).

One ``SymbolTFEngine`` mirrors ONE MT4 chart attachment (the v5 README:
attach per symbol x TF; each chart runs its own option family — the decay
monitor and rolling gate are per (symbol, TF, option) with the TF pool local
to the chart). State is cheap and recomputed per closed bar from candles +
the frozen artifact (the renko assessment note): every tick is IDEMPOTENT
over the fetched window, and only four pieces of state survive across ticks:

- ``frozen``: the per-setup gate decisions (EA g_frz*) — keyed by C2 open
  time, taken ONCE at arm time (C2 close) with the monitor state as of that
  close (no lookahead: labels resolve with bars up to the C2 bar), kept
  through C3;
- ``emitted``: C2 opens whose fills already landed on the paper book (dedup
  across re-fetches; the paper table is the durable record);
- ``missed``: C2 opens whose fill window passed unobserved (counted once);
- ``positions``: open paper-position resolution states (hydrated from the
  paper table on cold start — zero-migration; the row's ``venue`` carries
  the option key: ``crt:{tf}:{SESSION}``).

Per tick (EA ``OnNewTFBar`` order preserved):

1. detect RAW setups over the closed-bar window (positional C1 = i-1,
   C2 = i; the C3 bar is the EXACT-time bar at ``c2_open + tf`` — a gapped
   boundary stays PENDING forever, EA ``iBarShift`` semantics);
2. paper-label EVERY setup of a configured option with the option's settings
   (the ungated monitor stream — "options the optimizer disabled are
   monitored but never traded");
3. replay the decay monitor per option (labels after train_end only) and
   compute the 45d rolling stats (unfiltered) + the enabled-TF pool;
4. freeze gate decisions for setups whose C2 is the newest closed bar (C3
   forming — the correct arm moment) or, for C3C options only, the bar after
   (the EA's justClosed entry window; STOP/CONT late arms are blocked by the
   EA's AllowLateMarketEntry=false, so they are skipped honestly here);
5. emit paper fills for gated setups whose C3 just closed and whose stop
   filled during C3 (risk caps checked first; the ambiguous-C3 case opens
   AND resolves in the same tick);
6. resolve open positions bar by bar (the shared ``walk_barriers``).

Missed fill windows (engine down through C3) are skipped and counted once —
the EA would still hold its pending stop, but on bar data the fill-knowledge
moment passed; honest miss, no backfill.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import numpy as np

from crt import claims as crt_claims
from crt import paper
from crt.candles import to_arrays
from crt.config_loader import TF_SECONDS, OptionCfg
from crt.gates import ChopThresholds, RollingCfg, SizingCfg, execution_caps, freeze_decision
from crt.labeler import LabelResult, entry_bar_check, label_setup, walk_barriers
from crt.monitors import LabelRow, apply_monitor, apply_rolling, compute_rolling, replay_monitor
from crt.sessions import SessionWindows
from crt.setup import DIR_STR, SetupRecord, detect_setups

logger = logging.getLogger("crt.engine")

FROZEN_MAX = 256      # bounded per-setup decision memory (EA keeps one)
SESS_NAMES = {0: "OUT", 1: "NY", 2: "ASIA"}
SESS_IDS = {name: sid for sid, name in SESS_NAMES.items()}
MODEL_NAMES = {0: "STOP", 1: "C3C", 2: "CONT"}
_STATE_NAMES = {0: "NORMAL", 1: "CAUTION", 2: "OFF"}   # decay states (EM_*)


@dataclass
class PositionState:
    """An open CRT paper position's resolution state."""

    position_id: str
    signal_id: str                 # the claim's ledger sibling key (CRT-2)
    symbol: str
    tf: str
    session_id: int
    trade_buy: bool
    c3_open: float            # epoch — the entry (C3) bar's open time
    open_ts_epoch: float      # epoch — C3 close (the row's open_ts)
    entry_ref: float
    fill: float
    sl: float
    tp: float
    risk: float
    hold: int
    model: int
    cash_risk: float


@dataclass
class EngineStats:
    bars: int = 0
    setups: int = 0
    labeled: int = 0
    labels_pending: int = 0
    labels_closed: int = 0
    frozen_new: int = 0
    fills_emitted: int = 0
    positions_resolved: int = 0
    caps_blocked: int = 0
    cold_start_skipped: int = 0
    missed_fill_windows: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class SymbolTFEngine:
    """One (symbol, TF) CRT engine — one MT4 chart."""

    def __init__(self, symbol: str, tf: str,
                 options: dict[tuple[str, int], OptionCfg],
                 engine_cfg: dict[str, Any], spread: float, slip: float,
                 signal: Callable[[str, str, str], None] | None = None):
        self.symbol = symbol.upper()
        self.tf = tf
        self.options = options
        self.tf_seconds = TF_SECONDS[tf]
        self.spread = float(spread)
        self.slip = float(slip)
        self.signal = signal or (lambda *_: None)
        chop_cfg = engine_cfg.get("chop") or {}
        self.chop_thresholds = ChopThresholds.from_cfg(engine_cfg.get("chop"))
        self.chop_lookback = int(chop_cfg.get("lookback", 6))
        self.liquidity_lookback = int(engine_cfg.get("liquidity_lookback", 10))
        self.atr_period = int(engine_cfg.get("atr_period", 14))
        self.vol_lookback = int(engine_cfg.get("vol_lookback", 500))
        self.windows = SessionWindows.from_cfg(engine_cfg.get("sessions"))
        self.sizing = SizingCfg.from_cfg(engine_cfg.get("sizing"))
        self.roll = RollingCfg.from_cfg(engine_cfg.get("rolling"))
        self.decay_cfg = dict(engine_cfg.get("decay") or {})
        for key, default in (("kappa_frac", 0.5), ("kappa_min", 0.05),
                             ("h_caution", 12.0), ("h_off", 18.0),
                             ("roll_n", 60), ("roll_off_ev", -0.10),
                             ("react_n", 10)):
            self.decay_cfg.setdefault(key, default)
        self.decay_cfg.setdefault("use_monitor", True)
        self.decay_cfg.setdefault("caution_mult", 0.5)
        # CRT-2 claims hooks (engine_cfg['claims'] absent -> OFF: the CRT-1
        # contract holds byte-identical — claims are opt-in via config)
        self.claims_cfg = dict(engine_cfg.get("claims") or {})
        self.claims_enabled = bool(self.claims_cfg.get("enabled", False))
        self.artifact_runid = engine_cfg.get("artifact_runid")  # CRT-0 mint stamp
        # cross-tick state
        self.frozen: dict[float, Any] = {}
        self.emitted: set[float] = set()
        self.missed: set[float] = set()
        self.positions: dict[str, PositionState] = {}
        self.hydrated = False
        self.last_tick: dict[str, Any] = {"ts": None, "bars": 0, "setups": 0}

    # ── public surface ────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        """The /crt/state view: options with monitor state + open positions."""
        return {
            "symbol": self.symbol, "tf": self.tf,
            "options": [
                {"dir": o.dir, "session": SESS_NAMES.get(o.sess, o.sess),
                 "model": MODEL_NAMES.get(o.model, o.model),
                 "state": o.state_name(), "s_cusum": round(o.s_cusum, 2),
                 "live_n": o.live_n, "live_ev": round(o.live_ev, 3),
                 "roll_n": o.rl_n, "roll_ev": round(o.rl_ev, 3),
                 "oos_ev": o.base_ev, "oos_t": o.oos_t, "oos_n": o.oos_n,
                 "r": o.r, "hold": o.hold}
                for o in self.options.values()
            ],
            "open_positions": len(self.positions),
            "frozen_decisions": len(self.frozen),
            "last_tick": self.last_tick,
        }

    async def hydrate(self) -> None:
        """Cold start: rebuild open-position states from the paper table."""
        if self.hydrated:
            return
        self.hydrated = True
        try:
            rows = await paper.fetch_open_positions()
        except Exception as exc:  # noqa: BLE001 - fail-open
            logger.warning("%s %s: hydrate failed: %s", self.symbol, self.tf, exc)
            return
        for row in rows:
            try:
                state = self._position_state_from_row(row)
                if state is not None:
                    self.positions[state.position_id] = state
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("%s %s: hydrate row skipped: %s",
                               self.symbol, self.tf, exc)
        if self.positions:
            logger.info("%s %s: hydrated %d open CRT position(s)",
                        self.symbol, self.tf, len(self.positions))

    def _position_state_from_row(self, row: dict[str, Any]) -> PositionState | None:
        """Rebuild a PositionState from an open paper row (venue = the key).

        ``venue='crt:{tf}:{SESS}'`` + the trade direction recover the option
        (CONT inverts the setup direction; REV/C3C keep it). Ambiguity (a
        CONT candidate and a same-direction stop candidate both existing for
        the session) resolves toward CONT — the v5 primary; the row's TP
        distance cross-checks the option's fixed R.
        """
        if str(row.get("symbol", "")).upper() != self.symbol:
            return None
        parts = str(row.get("venue") or "").split(":")
        if len(parts) != 3 or parts[0] != "crt" or parts[1] != self.tf:
            return None
        sess = SESS_IDS.get(parts[2], -1)
        if sess < 0:
            return None
        direction = str(row.get("direction") or "").lower()
        if direction not in ("buy", "sell"):
            return None
        trade_buy = direction == "buy"
        inverted_dir = "SELL" if trade_buy else "BUY"
        kept_dir = "BUY" if trade_buy else "SELL"
        candidates = [self.options.get((inverted_dir, sess)),
                      self.options.get((kept_dir, sess))]
        candidates = [o for o in candidates if o is not None]
        option = next((o for o in candidates if o.model == 2), None) \
            or (candidates[0] if candidates else None)
        hold = option.hold if option else 24
        model = option.model if option else 2
        open_ts = datetime.fromisoformat(
            str(row.get("open_ts") or "").replace("Z", "+00:00")).timestamp()
        fill = float(row.get("entry_price") or 0.0)
        sl = float(row.get("stop_loss") or 0.0)
        tp = float(row.get("take_profit") or 0.0)
        if not fill or not sl:
            return None
        entry_ref = (fill - self.slip) if trade_buy else (fill + self.slip)
        risk = abs(entry_ref - sl)
        units = float(row.get("size_units") or 0.0)
        cash_risk = risk * units          # sizing basis: |entry_ref - sl| x units
        if option is not None and risk > 0 and abs(abs(tp - entry_ref) / risk - option.r) > 0.05 * max(1.0, option.r):
            logger.warning("%s %s: hydrated row %s TP distance off option R "
                           "(row carries its own frozen levels — resolving "
                           "with the row's)", self.symbol, self.tf,
                           str(row.get("position_id"))[:8])
        return PositionState(
            position_id=str(row.get("position_id")),
            signal_id=str(row.get("signal_id") or ""),
            symbol=self.symbol,
            tf=self.tf, session_id=sess, trade_buy=trade_buy,
            c3_open=open_ts - self.tf_seconds, open_ts_epoch=open_ts,
            entry_ref=entry_ref, fill=fill, sl=sl, tp=tp, risk=risk,
            hold=hold, model=model, cash_risk=cash_risk)

    async def on_candles(self, bars: list[dict[str, Any]], now_epoch: float,
                         risk_state: dict[str, Any] | None = None) -> dict[str, Any]:
        """One tick over CLOSED bars (ascending, open-time stamped)."""
        await self.hydrate()
        stats = EngineStats(bars=len(bars))
        self.last_tick = {"ts": now_epoch, "bars": len(bars), "setups": 0}
        if len(bars) < max(self.atr_period + 2, self.chop_lookback + 3):
            return stats.as_dict()
        risk_state = risk_state if risk_state is not None else {}
        t, o, h, low, c = to_arrays(bars)
        idx_of_t = {float(v): i for i, v in enumerate(t)}
        last_idx = t.size - 1

        # 1. detect RAW setups
        setups = detect_setups(
            t, o, h, low, c, self.tf_seconds, self.windows,
            chop_lookback=self.chop_lookback,
            liquidity_lookback=self.liquidity_lookback,
            atr_period=self.atr_period, vol_lookback=self.vol_lookback)
        stats.setups = len(setups)
        self.last_tick["setups"] = len(setups)

        # 2. paper-label every setup whose option exists (the monitor stream)
        rows: list[LabelRow] = []
        setup_labels: list[tuple[SetupRecord, OptionCfg, LabelResult]] = []
        for s in setups:
            option = self.options.get((DIR_STR[s.dir], s.session_id))
            if option is None:
                continue    # no option for this (dir, session): never counted
            c3_idx = idx_of_t.get(s.c3_open)
            label = label_setup(s, t, o, h, low, c, option, c3_idx,
                                self.spread, self.slip)
            rows.append(LabelRow(setup=s, option=option, label=label,
                                 c3_time=s.c3_open))
            setup_labels.append((s, option, label))
            stats.labeled += 1
            if label.closed:
                stats.labels_closed += 1
            elif label.outcome == 2:      # OUT_PENDING
                stats.labels_pending += 1

        # 3. monitors: decay replay per option + rolling window + TF pool
        by_option: dict[tuple[str, int], list[LabelRow]] = {}
        for row in rows:
            by_option.setdefault(row.option.key, []).append(row)
        for opt_rows in by_option.values():
            option = opt_rows[0].option
            prev_state = option.state
            apply_monitor(option, replay_monitor(option, opt_rows, self.decay_cfg))
            if option.enabled and option.state != prev_state:
                # decay transition — operationally material (the edge's
                # health verdict changed); bounded by transition rarity
                self.signal("monitor", f"{self.symbol} {self.tf} decay",
                            f"{option.dir} {SESS_NAMES.get(option.sess, option.sess)}: "
                            f"{_STATE_NAMES.get(prev_state, prev_state)} -> "
                            f"{option.state_name()} (S {option.s_cusum:.1f}, "
                            f"live EV {option.live_ev:+.2f}R n{option.live_n})")
        per, tf_roll = compute_rolling(rows, now_epoch, self.roll.roll_days)
        apply_rolling(self.options, per)

        # 4. freeze gate decisions at arm time (C2 == newest closed bar; the
        #    C3C justClosed window is the one bar after — the EA trades C3C
        #    at C3 close, STOP/CONT late arms are honestly skipped)
        for s, option, _label in setup_labels:
            arm_window = (s.c2_idx == last_idx
                          or (s.c2_idx == last_idx - 1 and option.model == 1))
            if arm_window and s.c2_open not in self.frozen:
                decision = freeze_decision(
                    s, option, option.state, tf_roll,
                    self.chop_thresholds, self.sizing, self.roll,
                    use_decay_monitor=bool(self.decay_cfg.get("use_monitor", True)),
                    now_epoch=s.c2_open + self.tf_seconds)
                # CRT-2: stamp the freeze-time monitor snapshot — the claim
                # records the decision INPUTS (no lookahead, no hindsight)
                decision.monitor = {
                    "state": option.state_name(),
                    "s_cusum": round(float(option.s_cusum), 6),
                    "live_n": int(option.live_n),
                    "live_ev": round(float(option.live_ev), 6),
                    "roll_ev": round(float(option.roll_ev), 6),
                    "rl_n": int(option.rl_n),
                    "rl_ev": round(float(option.rl_ev), 6),
                }
                if len(self.frozen) >= FROZEN_MAX:
                    self.frozen.pop(next(iter(self.frozen)))
                self.frozen[s.c2_open] = decision
                stats.frozen_new += 1
                if decision.passed:
                    self.signal("fill", f"{self.symbol} {self.tf} setup armed",
                                f"{DIR_STR[s.dir]} setup {option.state_name()} — "
                                f"gate PASSED ({decision.reason}), risk "
                                f"{decision.risk_pct:.2f}%")

        # 5. emit paper fills for gated setups whose C3 just closed
        for s, option, label in setup_labels:
            if s.c2_open in self.emitted:
                continue
            c3_idx = idx_of_t.get(s.c3_open)
            if c3_idx != last_idx:
                if (label.filled and c3_idx is not None and c3_idx < last_idx
                        and s.c2_open not in self.missed):
                    self.missed.add(s.c2_open)
                    if len(self.missed) > FROZEN_MAX:
                        self.missed = set(sorted(self.missed)[-FROZEN_MAX:])
                    stats.missed_fill_windows += 1
                continue
            decision = self.frozen.get(s.c2_open)
            if decision is None:
                stats.cold_start_skipped += 1
                continue
            if not decision.passed or not label.filled:
                continue    # gate failed, or the stop never touched — cancelled
            ok, why = execution_caps(risk_state, self.symbol, self.sizing)
            if not ok:
                stats.caps_blocked += 1
                self.signal("error", f"{self.symbol} fill blocked",
                            f"gate passed but caps blocked: {why}")
                self.emitted.add(s.c2_open)    # one attempt per setup, EA-style
                continue
            emitted = await self._emit_position(s, option, label, decision, stats)
            if emitted:
                self.emitted.add(s.c2_open)
                if len(self.emitted) > FROZEN_MAX:
                    self.emitted = set(sorted(self.emitted)[-FROZEN_MAX:])
            else:
                # insert failed: allow one natural retry while the C3 bar is
                # still the newest closed bar (TDVA lag), then the window passes
                continue

        # 6. resolve open positions
        await self._resolve_positions(t, h, low, c, idx_of_t, stats)
        return stats.as_dict()

    # ── internals ─────────────────────────────────────────────────────
    async def _emit_position(self, setup: SetupRecord, option: OptionCfg,
                             label: LabelResult, decision: Any,
                             stats: EngineStats) -> bool:
        cash_risk = self.sizing.paper_equity * decision.risk_pct / 100.0
        open_ts = setup.c3_open + self.tf_seconds    # C3 close — fill knowledge
        row = paper.build_open_row(
            symbol=self.symbol, tf=self.tf, session_id=setup.session_id,
            trade_buy=label.trade_buy, entry_price=label.fill_price,
            stop_loss=label.sl, take_profit=label.tp,
            cash_risk=cash_risk, open_ts_epoch=open_ts,
            risk_dist=abs(label.entry_ref - label.sl))
        if not await paper.open_position(row):
            self.signal("error", f"{self.symbol} paper insert failed",
                        f"position for C2 {setup.c2_open:.0f} not recorded")
            return False
        # CRT-2: mint the option claim (fail-open — a lost stamp is a lost
        # stamp; the paper book stays the durable record). Awaited BEFORE
        # the ambiguous-C3 close so mint/resolve can never race on the same
        # signal_id.
        claim_minted = False
        if self.claims_enabled:
            claim_row = crt_claims.build_option_claim(
                symbol=self.symbol, tf=self.tf, option=option,
                decision=decision, label=label, position_row=row,
                artifact_runid=self.artifact_runid, open_ts_epoch=open_ts,
                cash_risk=cash_risk,
                source=str(self.claims_cfg.get("source")
                           or crt_claims.CRT_CLAIM_SOURCE))
            claim_minted = await crt_claims.mint_option_claim(claim_row)
        state = PositionState(
            position_id=row["position_id"],
            signal_id=str(row.get("signal_id") or ""),
            symbol=self.symbol, tf=self.tf,
            session_id=setup.session_id, trade_buy=label.trade_buy,
            c3_open=setup.c3_open, open_ts_epoch=open_ts,
            entry_ref=label.entry_ref, fill=label.fill_price, sl=label.sl,
            tp=label.tp, risk=label.risk, hold=option.hold, model=option.model,
            cash_risk=cash_risk)
        self.positions[state.position_id] = state    # live resolution tracking
        stats.fills_emitted += 1
        self.signal("fill", f"{self.symbol} paper fill",
                    f"{self.tf} {'BUY' if label.trade_buy else 'SELL'} "
                    f"{option.state_name()} | entry {label.fill_price:.5g} "
                    f"SL {label.sl:.5g} TP {label.tp:.5g} ({option.r:g}R, "
                    f"hold {option.hold}) | risk {decision.risk_pct:.2f}% "
                    f"= {cash_risk:.0f} | {decision.reason}"
                    + ("" if claim_minted else " | claim NOT minted (fail-open)"))
        # the ambiguous-C3 case: resolved within the entry bar itself
        if label.closed:
            await self._close_position(state, label, stats)
        return True

    async def _resolve_positions(self, t: np.ndarray, h: np.ndarray,
                                 low: np.ndarray, c: np.ndarray,
                                 idx_of_t: dict[float, int],
                                 stats: EngineStats) -> None:
        if not self.positions:
            return
        for position_id in list(self.positions):
            state = self.positions[position_id]
            c3_idx = idx_of_t.get(state.c3_open)
            if c3_idx is None:
                continue    # entry bar outside this window — nothing to walk
            walked: LabelResult | None = None
            if state.model != 1:
                # entry-bar (C3) checks — idempotent; live-emitted positions
                # already closed if this resolved at emission, hydrated ones
                # (crash between emission and C3 close) catch it here
                walked = entry_bar_check(
                    state.trade_buy, state.fill, state.sl, state.tp,
                    state.risk, float(h[c3_idx]), float(low[c3_idx]),
                    self.spread, self.slip)
            if walked is None:
                walked = walk_barriers(
                    state.trade_buy, state.fill, state.sl, state.tp, state.risk,
                    c3_idx, t, h, low, c, state.hold, self.spread, self.slip)
            if walked.closed:
                await self._close_position(state, walked, stats)

    async def _close_position(self, state: PositionState, label: LabelResult,
                              stats: EngineStats) -> None:
        close_ts = state.c3_open + self.tf_seconds * (1 + label.held_bars)
        patch = paper.build_close_patch(
            outcome=label.outcome, r=label.r, exit_price=label.exit_price,
            cash_risk=state.cash_risk, close_ts_epoch=close_ts)
        ok = await paper.patch_position(state.position_id, patch)
        self.positions.pop(state.position_id, None)
        if ok:
            stats.positions_resolved += 1
            # CRT-2: resolve the position's claim through the SAME verdict
            # (fail-open; guarded by status=eq.open — a voided claim is
            # never re-touched)
            claim_resolved = False
            if self.claims_enabled and state.signal_id:
                outcome = crt_claims.build_claim_outcome(
                    label, cash_risk=state.cash_risk, close_ts_epoch=close_ts)
                claim_resolved = await crt_claims.resolve_position_claim(
                    state.signal_id, outcome)
            self.signal("resolve", f"{self.symbol} paper resolved",
                        f"{self.tf} {state.position_id[:8]} -> "
                        f"{label.outcome_name} {label.r:+.3f}R "
                        f"({patch['realized_pnl']:+.2f})"
                        + ("" if claim_resolved or not self.claims_enabled
                           else " | claim NOT resolved (fail-open)"))
        else:
            self.signal("error", f"{self.symbol} paper patch failed",
                        f"position {state.position_id[:8]} "
                        f"{label.outcome_name} NOT recorded — dropped from "
                        f"memory (will not retry)")
