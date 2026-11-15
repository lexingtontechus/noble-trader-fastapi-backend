"""labeler.py — triple-barrier R labeling (CRT-1, port of EA ``LabelCustomR``).

Labels one setup with one option's settings — the SAME function serves both
uses the EA puts it to: the paper-label stream that feeds the decay monitor
and rolling-EV gate (every raw setup of a configured option, ungated), and
the resolution walk for emitted paper positions.

Verbatim port (MQL4 shifts -> ascending closed-bar indices; ``shift s`` of a
closed series maps to array index ``len - s``):

- trade direction: CONT (model 2) trades OPPOSITE the setup; REV/C3C trade
  the setup direction (EA ``TradeBuy``);
- SL (all models): buy -> ``c2_low - sb*atr`` / sell -> ``c2_high + sb*atr``;
- entry reference: stop models (0/2) -> the C2 sweep extreme +/− eb*atr,
  filled intrabar during C3 (buy: ``h3 + spread >= entry``); C3C (1) -> the
  C3 close with confirmation conditions (sweep extreme exceeded, close beyond
  SL, optional directional close);
- costs: buy fill = entry + slip (+ spread for C3C market entry); SL exit
  worsened by slip; sell exits add the spread (chart prices are bid);
- the C3 bar itself is checked FIRST for stop models — SL before TP
  (same-bar tie -> SL, conservative; the "ambiguous C3" case), then bars
  held=1..hold after C3, SL before TP on each;
- not enough closed bars yet -> PENDING (re-checked on every new bar);
- time exit at the option's hold: close of the hold-th bar after C3 (sell
  exits on ask = close + spread).

The C3 bar is matched by EXACT open time (the EA's ``iBarShift(..., true)``)
— a session/weekend gap where no bar opened at the C3 boundary leaves the
setup permanently PENDING, exactly like the EA.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from crt.config_loader import OptionCfg
from crt.setup import SetupRecord

OUT_PENDING = 2
OUT_TP = 1
OUT_TIME = 0
OUT_SL = -1
OUT_NOFILL = 3

OUTCOME_NAMES = {OUT_TP: "TP", OUT_SL: "SL", OUT_TIME: "TIME",
                 OUT_NOFILL: "NOFILL", OUT_PENDING: "PENDING"}

CLOSED_OUTCOMES = (OUT_TP, OUT_SL, OUT_TIME)


@dataclass
class LabelResult:
    """One labeled setup (outcome codes above; ``r`` in R multiples).

    ``exit_price`` is the barrier's own exit price (SL with slip, TP level,
    or the hold-bar close; sell exits carry the spread). ``filled`` is True
    for every outcome except NOFILL and the nothing-known PENDING — a
    mid-walk PENDING (barriers unresolved) carries the trade geometry.
    """

    outcome: int
    r: float
    held_bars: int
    trade_buy: bool
    entry_ref: float
    fill_price: float
    sl: float
    tp: float
    risk: float
    filled: bool
    exit_price: float | None = None

    @property
    def outcome_name(self) -> str:
        return OUTCOME_NAMES.get(self.outcome, "?")

    @property
    def closed(self) -> bool:
        return self.outcome in CLOSED_OUTCOMES


def trade_buy_of(setup_dir: int, model: int) -> bool:
    """EA TradeBuy: CONT (model 2) trades opposite the setup direction."""
    return (setup_dir == -1) if model == 2 else (setup_dir == 1)


def walk_barriers(buy: bool, fill: float, sl: float, tp: float, risk: float,
                  c3_idx: int, t: np.ndarray, h: np.ndarray, low: np.ndarray,
                  c: np.ndarray, hold: int, spread: float,
                  slip: float) -> LabelResult:
    """The shared held=1..hold SL-first walk + time exit (EA LabelCustomR tail).

    Used by ``label_setup`` AND by the engine's open-position resolver — one
    barrier semantics, two callers. Returns OUT_PENDING while the hold window
    has not closed yet. SL exit price worsened by slip; sell exits on ask
    (chart bid + spread).
    """
    sp = spread
    sl_exit = (sl - slip) if buy else (sl + slip)
    last_idx = t.size - 1

    def _sl_out(held: int) -> LabelResult:
        r = ((sl_exit - fill) / risk) if buy else ((fill - sl_exit) / risk)
        return LabelResult(OUT_SL, float(r), held, buy, 0.0, fill, sl, tp,
                           risk, True, sl_exit)

    def _tp_out(held: int) -> LabelResult:
        r = ((tp - fill) / risk) if buy else ((fill - tp) / risk)
        return LabelResult(OUT_TP, float(r), held, buy, 0.0, fill, sl, tp,
                           risk, True, tp)

    def _pending() -> LabelResult:
        return LabelResult(OUT_PENDING, 0.0, 0, buy, 0.0, fill, sl, tp,
                           risk, True, None)

    for held in range(1, hold + 1):
        a = c3_idx + held
        if a > last_idx:
            return _pending()
        hh, ll = float(h[a]), float(low[a])
        fav = hh if buy else (ll + sp)
        adv = ll if buy else (hh + sp)
        if (adv <= sl) if buy else (adv >= sl):
            return _sl_out(held)
        if (fav >= tp) if buy else (fav <= tp):
            return _tp_out(held)

    a = c3_idx + hold
    if a > last_idx:
        return _pending()
    exit_px = float(c[a]) if buy else (float(c[a]) + sp)
    r = ((exit_px - fill) / risk) if buy else ((fill - exit_px) / risk)
    return LabelResult(OUT_TIME, float(r), hold, buy, 0.0, fill, sl, tp,
                       risk, True, exit_px)


def entry_bar_check(buy: bool, fill: float, sl: float, tp: float, risk: float,
                    h3: float, l3: float, spread: float,
                    slip: float) -> LabelResult | None:
    """The C3-bar (entry bar) barrier check for stop models (EA fav0/adv0).

    SL before TP — the conservative same-bar tie ("ambiguous C3": the fill
    and the stop both inside C3 -> assume SL). Returns None when neither
    barrier touched. Used by the resolver for HYDRATED positions (the
    emission-time label already ran this for live-emitted ones).
    """
    sp = spread
    sl_exit = (sl - slip) if buy else (sl + slip)
    fav0 = h3 if buy else (l3 + sp)
    adv0 = l3 if buy else (h3 + sp)
    if (adv0 <= sl) if buy else (adv0 >= sl):
        r = ((sl_exit - fill) / risk) if buy else ((fill - sl_exit) / risk)
        return LabelResult(OUT_SL, float(r), 0, buy, 0.0, fill, sl, tp,
                           risk, True, sl_exit)
    if (fav0 >= tp) if buy else (fav0 <= tp):
        r = ((tp - fill) / risk) if buy else ((fill - tp) / risk)
        return LabelResult(OUT_TP, float(r), 0, buy, 0.0, fill, sl, tp,
                           risk, True, tp)
    return None


def label_setup(setup: SetupRecord, t: np.ndarray, o: np.ndarray, h: np.ndarray,
                low: np.ndarray, c: np.ndarray, option: OptionCfg,
                c3_idx: int | None, spread: float, slip: float) -> LabelResult:
    """Label one setup with one option. ``c3_idx``: exact C3 bar index or None."""
    buy = trade_buy_of(setup.dir, option.model)
    atr, sp = setup.atr, spread
    sl = (setup.c2_low - option.sb * atr) if buy else (setup.c2_high + option.sb * atr)

    def _pending(entry_ref: float = 0.0, fill: float = 0.0, tp: float = 0.0,
                 risk: float = 0.0, filled: bool = False) -> LabelResult:
        # PENDING: either the C3 bar is not closed yet (nothing known), or the
        # trade FILLED during C3 but its barriers have not resolved with the
        # bars closed so far (filled=True, geometry carried for the engine).
        return LabelResult(OUT_PENDING, 0.0, 0, buy, entry_ref, fill, sl, tp,
                           risk, filled, None)

    if c3_idx is None or c3_idx < 0 or c3_idx > t.size - 1:
        return _pending()
    o3, h3, l3, c3 = (float(o[c3_idx]), float(h[c3_idx]),
                      float(low[c3_idx]), float(c[c3_idx]))

    model = option.model
    if model != 1:      # REV (0) and CONT (2): stop order inside C3
        entry_ref = (setup.c2_high + option.eb * atr) if buy else (setup.c2_low - option.eb * atr)
        filled = (h3 + sp >= entry_ref) if buy else (l3 <= entry_ref)
    else:               # C3C: market entry at C3 close with confirmation
        entry_ref = c3
        filled = (h3 > setup.c2_high) if buy else (l3 < setup.c2_low)
        filled = filled and ((c3 > sl) if buy else (c3 < sl))
        if option.c3_dir == 1:
            filled = filled and ((c3 > o3) if buy else (c3 < o3))

    risk = (entry_ref - sl) if buy else (sl - entry_ref)
    if not filled or risk <= 0:
        return LabelResult(OUT_NOFILL, 0.0, 0, buy, entry_ref, 0.0, sl, 0.0,
                           0.0, False, None)

    fill = (entry_ref + slip + (sp if model == 1 else 0.0)) if buy else (entry_ref - slip)
    sl_exit = (sl - slip) if buy else (sl + slip)
    tp = (entry_ref + option.r * risk) if buy else (entry_ref - option.r * risk)

    def _sl_out(held: int) -> LabelResult:
        r = ((sl_exit - fill) / risk) if buy else ((fill - sl_exit) / risk)
        return LabelResult(OUT_SL, float(r), held, buy, entry_ref, fill, sl, tp,
                           risk, True, sl_exit)

    def _tp_out(held: int) -> LabelResult:
        r = ((tp - fill) / risk) if buy else ((fill - tp) / risk)
        return LabelResult(OUT_TP, float(r), held, buy, entry_ref, fill, sl, tp,
                           risk, True, tp)

    # C3 bar itself (stop models only): SL before TP — conservative tie.
    if model != 1:
        fav0 = h3 if buy else (l3 + sp)
        adv0 = l3 if buy else (h3 + sp)
        if (adv0 <= sl) if buy else (adv0 >= sl):
            return _sl_out(0)
        if (fav0 >= tp) if buy else (fav0 <= tp):
            return _tp_out(0)

    # Bars after C3: held = 1..hold, SL before TP on each bar (the shared
    # walk — the same function resolves open paper positions later).
    walked = walk_barriers(buy, fill, sl, tp, risk, c3_idx, t, h, low, c,
                           option.hold, sp, slip)
    if walked.outcome == OUT_PENDING:
        return _pending(entry_ref, fill, tp, risk, True)
    walked.entry_ref = entry_ref
    return walked
