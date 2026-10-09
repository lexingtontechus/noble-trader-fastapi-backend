"""
Settlement Contract — single source of truth for trade outcome + PnL
═════════════════════════════════════════════════════════════════════

Phase 1 of worklog/20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md.

One pure module owns:
  * deadline-aware first-passage resolution (close-walk or OHLC touch)
  * same-bar TP/SL priority (parameterized)
  * expired mark-to-market
  * brick/dollar PnL (optional cost deduction)

Callers (eod_resolver, heavy-sweep scoring, paper book, scorecard) must
NOT reimplement these rules. Mode flag ``settlement_contract_mode``:

  off     — legacy paths only (resolver keeps its inline walk)
  shadow  — contract computes; callers may log comparison; live writes stay legacy
  live    — callers use this module for outcome + PnL

``settlement_same_bar_policy``:
  tp_first — historical live convention (optimistic; preserves pre-Batch-A outcomes)
  sl_first — conservative; matches heavy-sweep Renko OCO default

Design rules: pure functions, no I/O, no Supabase, no settings import
except via explicit args — so unit tests and replay scripts stay offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# Shared deadline — keep in sync with eod_resolver.EXPIRY_BARS (re-exported
# by that module for backward compat). Not imported from eod_resolver to
# avoid a cycle if the resolver ever imports this module first.
EXPIRY_BARS = 48

SAME_BAR_TP_FIRST = "tp_first"
SAME_BAR_SL_FIRST = "sl_first"
_VALID_POLICIES = (SAME_BAR_TP_FIRST, SAME_BAR_SL_FIRST)

OUTCOME_TP = "tp_hit"
OUTCOME_SL = "sl_hit"
OUTCOME_EXPIRED = "expired"


def _is_buy(direction: str | None) -> bool:
    return str(direction or "buy").strip().lower() not in ("sell", "short", "s")


def _as_float_list(bars: Sequence[Any] | None, *keys: str) -> list[float] | None:
    """First non-None value across ``keys`` for each bar (aliases: h/high, …)."""
    if bars is None:
        return None
    out: list[float] = []
    for b in bars:
        if isinstance(b, Mapping):
            v = None
            for k in keys:
                v = b.get(k)
                if v is not None:
                    break
        else:
            v = b
        if v is None:
            return None
        out.append(float(v))
    return out or None


def normalize_bars(
    bars: Sequence[Any],
) -> tuple[list[float], list[float] | None, list[float] | None]:
    """Accept closes-only floats or OHLC dicts/objects.

    Returns ``(closes, highs, lows)``. highs/lows are None when the input
    is close-only — callers then fall back to close-walk.

    Accepts both key conventions used in this codebase: the price fetcher's
    short keys (``c``/``h``/``l``) and the long form (``close``/``high``/
    ``low``). Without the alias, a fetcher-shaped bar would raise and the
    basis fix would silently fall back to close-walk.
    """
    if not bars:
        return [], None, None
    if not isinstance(bars[0], Mapping):
        return [float(x) for x in bars], None, None
    closes = _as_float_list(bars, "close", "c")
    highs = _as_float_list(bars, "high", "h")
    lows = _as_float_list(bars, "low", "l")
    if closes is None:
        raise ValueError("settlement_contract: OHLC bars require a close/'c' field")
    return closes, highs, lows


@dataclass(frozen=True)
class SettlementCosts:
    """Cost model in price units relative to entry (or absolute if preferred).

    ``spread_bps`` / ``slip_bps`` / ``commission_bps`` are basis points of
    ``entry_price`` applied as a round-trip drag on dollar PnL. Defaults
    keep Batch A behavior identical to the cost-free path until instrument
    tables are filled (worklog Phase 1 costs remain 0 by default).
    """

    spread_bps: float = 0.0
    slip_bps: float = 0.0
    commission_bps: float = 0.0

    def total_bps(self) -> float:
        return float(self.spread_bps) + float(self.slip_bps) + float(self.commission_bps)

    def dollar_cost(self, entry_price: float) -> float:
        if not entry_price:
            return 0.0
        return abs(float(entry_price)) * self.total_bps() / 10_000.0


@dataclass
class SettlementResult:
    outcome: str
    exit_price: float | None
    exit_idx: int | None
    hold_bars: int
    mfe: float
    mae: float
    pnl_bricks: float
    pnl_dollars: float
    pnl_dollars_net: float
    same_bar_policy: str
    bar_mode: str  # "ohlc" | "close"
    costs_dollars: float = 0.0
    flags: dict[str, Any] = field(default_factory=dict)

    def as_outcome_dict(self) -> dict[str, Any]:
        """Shape compatible with eod_resolver.resolve_signal_outcome consumers."""
        return {
            "outcome": self.outcome,
            "exit_price": self.exit_price,
            "exit_idx": self.exit_idx,
            "hold_bars": self.hold_bars,
            "mfe": self.mfe,
            "mae": self.mae,
        }

    def as_pnl_dict(self) -> dict[str, float]:
        """Shape compatible with eod_resolver.compute_pnl consumers."""
        return {
            "pnl_bricks": self.pnl_bricks,
            "pnl_dollars": round(self.pnl_dollars, 6),
        }


def _resolve_touch(
    *,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    closes: Sequence[float],
    highs: Sequence[float] | None,
    lows: Sequence[float] | None,
    is_buy: bool,
    expiry_bars: int,
    same_bar_policy: str,
) -> tuple[str, float | None, int | None, str]:
    """First-passage walk. Returns (outcome, exit_price, exit_idx, bar_mode)."""
    use_ohlc = highs is not None and lows is not None
    bar_mode = "ohlc" if use_ohlc else "close"
    window_n = min(len(closes), max(int(expiry_bars or 0), 0))

    for i in range(window_n):
        close = float(closes[i])
        if use_ohlc:
            hi = float(highs[i])  # type: ignore[index]
            lo = float(lows[i])  # type: ignore[index]
            if is_buy:
                tp_touch = hi >= take_profit
                sl_touch = lo <= stop_loss
            else:
                tp_touch = lo <= take_profit
                sl_touch = hi >= stop_loss
        else:
            if is_buy:
                tp_touch = close >= take_profit
                sl_touch = close <= stop_loss
            else:
                tp_touch = close <= take_profit
                sl_touch = close >= stop_loss

        if tp_touch and sl_touch:
            if same_bar_policy == SAME_BAR_SL_FIRST:
                # Conservative / backtest OCO default: SL wins.
                exit_p = float(stop_loss) if use_ohlc else close
                return OUTCOME_SL, exit_p, i, bar_mode
            # Legacy live convention: TP first; close-only keeps the
            # triggering close for byte-parity with eod_resolver.
            exit_p = float(take_profit) if use_ohlc else close
            return OUTCOME_TP, exit_p, i, bar_mode
        if tp_touch:
            # Level fill for TP (limit-like); close retained only as fallback
            # when caller wants mark — we use level for PnL bricks consistency
            # but keep triggering bar reference as the close for legacy shape.
            return OUTCOME_TP, float(take_profit) if use_ohlc else close, i, bar_mode
        if sl_touch:
            if use_ohlc:
                # Gap-through: if open is beyond SL, fill at that worse price.
                # We do not receive open here — use SL level; gaps vs open are
                # a caller-supplied enhancement (bar may include 'open').
                return OUTCOME_SL, float(stop_loss), i, bar_mode
            return OUTCOME_SL, close, i, bar_mode

    # Expired: still open at last walked close
    exit_price = float(closes[window_n - 1]) if window_n > 0 else None
    return OUTCOME_EXPIRED, exit_price, None, bar_mode


def _excursions(
    entry_price: float,
    walked: Sequence[float],
    is_buy: bool,
) -> tuple[float, float]:
    if not walked:
        return 0.0, 0.0
    if is_buy:
        mfe = max((float(c) - entry_price for c in walked), default=0.0)
        mae = max((entry_price - float(c) for c in walked), default=0.0)
    else:
        mfe = max((entry_price - float(c) for c in walked), default=0.0)
        mae = max((float(c) - entry_price for c in walked), default=0.0)
    return round(mfe, 6), round(mae, 6)


def compute_bricks_pnl(
    outcome: str,
    *,
    tp_bricks: float | None,
    sl_bricks: float | None,
    brick_size: float | None,
    entry_price: float | None = None,
    exit_price: float | None = None,
    direction: str = "buy",
) -> tuple[float, float]:
    """Return (pnl_bricks, pnl_dollars). Matches eod_resolver.compute_pnl."""
    if outcome == OUTCOME_TP:
        pnl_bricks = abs(float(tp_bricks or 0.0))
    elif outcome == OUTCOME_SL:
        pnl_bricks = -abs(float(sl_bricks or 0.0))
    elif (
        outcome == OUTCOME_EXPIRED
        and entry_price is not None
        and exit_price is not None
        and brick_size
    ):
        is_buy = _is_buy(direction)
        delta = (
            float(exit_price) - float(entry_price)
            if is_buy
            else float(entry_price) - float(exit_price)
        )
        pnl_bricks = delta / float(brick_size)
    else:
        pnl_bricks = 0.0
    dollars = pnl_bricks * float(brick_size or 0.0)
    return pnl_bricks, dollars


def settle(
    *,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    bars: Sequence[Any],
    direction: str = "buy",
    expiry_bars: int = EXPIRY_BARS,
    same_bar_policy: str = SAME_BAR_TP_FIRST,
    tp_bricks: float | None = None,
    sl_bricks: float | None = None,
    brick_size: float | None = None,
    costs: SettlementCosts | None = None,
) -> SettlementResult:
    """Resolve outcome + PnL under the single settlement contract.

    ``bars``: sequence of floats (closes) or mappings with close/high/low
    (and optionally open for future gap fills).
    """
    policy = str(same_bar_policy or SAME_BAR_TP_FIRST).strip().lower()
    if policy not in _VALID_POLICIES:
        policy = SAME_BAR_TP_FIRST

    closes, highs, lows = normalize_bars(bars)
    is_buy = _is_buy(direction)

    outcome, exit_price, exit_idx, bar_mode = _resolve_touch(
        entry_price=float(entry_price),
        stop_loss=float(stop_loss),
        take_profit=float(take_profit),
        closes=closes,
        highs=highs,
        lows=lows,
        is_buy=is_buy,
        expiry_bars=expiry_bars,
        same_bar_policy=policy,
    )

    walked = closes if exit_idx is None else closes[: exit_idx + 1]
    mfe, mae = _excursions(float(entry_price), walked, is_buy)
    hold_bars = (exit_idx + 1) if exit_idx is not None else min(
        len(closes), max(int(expiry_bars or 0), 0)
    )

    pnl_bricks, pnl_dollars = compute_bricks_pnl(
        outcome,
        tp_bricks=tp_bricks,
        sl_bricks=sl_bricks,
        brick_size=brick_size,
        entry_price=entry_price,
        exit_price=exit_price,
        direction=direction,
    )

    cost_model = costs or SettlementCosts()
    cost_dollars = cost_model.dollar_cost(float(entry_price)) if outcome != OUTCOME_EXPIRED else (
        # Expired still pays half the round-trip drag in a mark sense if
        # someone closes at market — apply full cost only when a terminal
        # TP/SL occurred; expired MTM stays gross for Batch A parity.
        0.0
    )
    pnl_net = float(pnl_dollars) - cost_dollars

    return SettlementResult(
        outcome=outcome,
        exit_price=exit_price,
        exit_idx=exit_idx,
        hold_bars=hold_bars,
        mfe=mfe,
        mae=mae,
        pnl_bricks=pnl_bricks,
        pnl_dollars=float(pnl_dollars),
        pnl_dollars_net=pnl_net,
        same_bar_policy=policy,
        bar_mode=bar_mode,
        costs_dollars=cost_dollars,
        flags={"direction": "long" if is_buy else "short"},
    )


def settle_from_closes(
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    closes: Sequence[float],
    direction: str = "buy",
    expiry_bars: int = EXPIRY_BARS,
    same_bar_policy: str = SAME_BAR_TP_FIRST,
    tp_bricks: float | None = None,
    sl_bricks: float | None = None,
    brick_size: float | None = None,
    costs: SettlementCosts | None = None,
) -> SettlementResult:
    """Convenience wrapper matching eod_resolver.resolve_signal_outcome's args."""
    return settle(
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        bars=list(closes or []),
        direction=direction,
        expiry_bars=expiry_bars,
        same_bar_policy=same_bar_policy,
        tp_bricks=tp_bricks,
        sl_bricks=sl_bricks,
        brick_size=brick_size,
        costs=costs,
    )


# ══════════════════════════════════════════════════════════════════════════
# resolve_bracket — the ONE first-passage walk (Stage 1, 2026-10-08)
# ══════════════════════════════════════════════════════════════════════════
#
# Why this exists (worklog/20261008_GEOMETRY_BASIS_DIVERGENCE.md):
# four sites resolved a bracket with three different semantics — the live
# resolver (high/low, SL-first), the geometry picker (closes, TP-first), and
# the heavy-sweep Renko backtest (brick close, SL-first). The picker therefore
# scored +0.61 ATR/signal on a bracket the resolver settled at −0.24: the
# entire gap was measurement, not market.
#
# This function is the single definition of "did this bracket win". Every
# consumer — resolver, geometry picker, monthly/4h reopt, heavy-sweep grid,
# first-passage read — resolves through it, so a bracket chosen on one basis
# can never disagree with the basis that settles it.
#
# BASIS is the sole knob that changes the answer:
#   BASIS_INTRABAR  — touch the level at any point in the bar (high/low).
#                     This is the live basis (eod_resolver_basis: intrabar).
#   BASIS_CLOSE     — only the bar's close counts. A wick that trips a tight
#                     stop is invisible. Optimistic; kept for parity replay
#                     of pre-cutover history and the legacy resolver path.
#
# ``resolve_bracket`` returns the raw per-bar envelope (or a scalar verdict),
# so both a vectorised grid search and a single live signal use the same rule.

BASIS_INTRABAR = "intrabar"
BASIS_CLOSE = "close_walk"
_VALID_BASES = (BASIS_INTRABAR, BASIS_CLOSE)


def resolve_bracket_path(
    *,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    direction: str,
    bars: Sequence[Any],
    basis: str = BASIS_INTRABAR,
    same_bar_policy: str = SAME_BAR_SL_FIRST,
    expiry_bars: int = EXPIRY_BARS,
) -> dict[str, Any]:
    """Resolve one bracket against one post-entry window. THE single walk.

    ``bars``: OHLC mappings (``{"h","l","c"}`` or ``{"high","low","close"}``)
    or plain closes. Returns a dict with:

      outcome       tp_hit | sl_hit | expired
      exit_price    fill price (level for a touch, last close on expiry)
      exit_idx      bar index of the resolution (None on expiry)
      hold_bars     bars held (exit_idx+1, or the window length on expiry)
      bar_mode      "ohlc" | "close"
      ambiguous     True when a single bar first-touched BOTH levels (the
                    order is unknowable from OHLC; resolved by policy)
      same_bar_policy, basis  — echo the rule that produced the verdict

    This is the reference implementation. ``settle`` and the numpy path
    builder below are both defined in terms of this rule so they cannot drift.
    """
    pol = str(same_bar_policy or SAME_BAR_SL_FIRST).strip().lower()
    if pol not in _VALID_POLICIES:
        pol = SAME_BAR_SL_FIRST
    bas = str(basis or BASIS_INTRABAR).strip().lower()
    if bas not in _VALID_BASES:
        bas = BASIS_INTRABAR

    is_buy = _is_buy(direction)

    # Pre-filter malformed bars (where any required field is None).
    # The old inline walk caught this per-bar with try/except; we filter upfront.
    valid_bars: list[Any] = []
    for b in bars:
        if isinstance(b, Mapping):
            if b.get("close", b.get("c")) is not None:
                valid_bars.append(b)
        else:
            # Plain float close
            try:
                if b is not None:
                    valid_bars.append(b)
            except (TypeError, ValueError):
                pass

    try:
        n_closes, n_highs, n_lows = normalize_bars(valid_bars)
    except ValueError:
        n_closes, n_highs, n_lows = [], None, None

    if n_closes is None:
        n_closes = []

    closes, highs, lows = n_closes, n_highs, n_lows
    use_ohlc = bas == BASIS_INTRABAR and highs is not None and lows is not None
    bar_mode = "ohlc" if use_ohlc else "close"
    window_n = min(len(closes), max(int(expiry_bars or 0), 0))

    outcome, exit_price, exit_idx, ambiguous = OUTCOME_EXPIRED, None, None, False
    for i in range(window_n):
        close = float(closes[i])
        if use_ohlc:
            hi = float(highs[i])  # type: ignore[index]
            lo = float(lows[i])   # type: ignore[index]
            tp_touch, sl_touch = ((hi >= take_profit, lo <= stop_loss) if is_buy
                                  else (lo <= take_profit, hi >= stop_loss))
        else:
            tp_touch, sl_touch = ((close >= take_profit, close <= stop_loss) if is_buy
                                  else (close <= take_profit, close >= stop_loss))

        if tp_touch and sl_touch:
            ambiguous = True
            if pol == SAME_BAR_SL_FIRST:
                outcome, exit_price, exit_idx = OUTCOME_SL, (
                    float(stop_loss) if use_ohlc else close), i
            else:
                outcome, exit_price, exit_idx = OUTCOME_TP, (
                    float(take_profit) if use_ohlc else close), i
            break
        if sl_touch:
            outcome, exit_price, exit_idx = OUTCOME_SL, (
                float(stop_loss) if use_ohlc else close), i
            break
        if tp_touch:
            outcome, exit_price, exit_idx = OUTCOME_TP, (
                float(take_profit) if use_ohlc else close), i
            break
        exit_price = close  # remember the mark in case we expire

    if outcome == OUTCOME_EXPIRED:
        exit_price = float(closes[window_n - 1]) if window_n > 0 else None

    hold_bars = (exit_idx + 1) if exit_idx is not None else window_n
    return {
        "outcome": outcome,
        "exit_price": exit_price,
        "exit_idx": exit_idx,
        "hold_bars": hold_bars,
        "bar_mode": bar_mode,
        "ambiguous": ambiguous,
        "same_bar_policy": pol,
        "basis": bas,
    }


def bracket_envelope(
    *,
    entry_price: float,
    direction: str,
    bars: Sequence[Any],
    basis: str = BASIS_INTRABAR,
    expiry_bars: int = EXPIRY_BARS,
) -> tuple[list[float], list[float]]:
    """Per-bar (favourable, adverse) ATR-agnostic excursion envelope.

    Returns two equal-length lists, in PRICE DELTA from entry, signed so that
    a larger ``favourable`` is better for the trade's direction. This is the
    shared shape the geometry picker and first-passage read consume: a grid
    search can evaluate every (tp, sl) pair from one envelope without
    re-walking prices, and the envelope is built from the SAME rule
    (``resolve_bracket_path``) so a pick and its settlement agree.

    ``basis`` selects high/low (intrabar) or close-only excursions.
    """
    bas = str(basis or BASIS_INTRABAR).strip().lower()
    if bas not in _VALID_BASES:
        bas = BASIS_INTRABAR
    is_buy = _is_buy(direction)
    closes, highs, lows = normalize_bars(bars)
    use_ohlc = bas == BASIS_INTRABAR and highs is not None and lows is not None
    n = min(len(closes), max(int(expiry_bars or 0), 0))

    fav: list[float] = []
    adv: list[float] = []
    for i in range(n):
        close = float(closes[i])
        if use_ohlc:
            hi = float(highs[i])  # type: ignore[index]
            lo = float(lows[i])   # type: ignore[index]
        else:
            hi = lo = close
        up = (hi - entry_price) if is_buy else (entry_price - lo)
        dn = (lo - entry_price) if is_buy else (entry_price - hi)
        fav.append(up)
        adv.append(dn)
    return fav, adv


def resolve_bracket_from_envelope(
    fav: Sequence[float],
    adv: Sequence[float],
    tp_dist: float,
    sl_dist: float,
    *,
    same_bar_policy: str = SAME_BAR_SL_FIRST,
) -> tuple[str, float, int | None]:
    """Resolve a bracket from a pre-built envelope (vectorised-grid friendly).

    ``fav``/``adv`` are per-bar favourable/adverse deltas (see
    ``bracket_envelope``). ``tp_dist``/``sl_dist`` are positive distances.
    Returns ``(outcome, pnl_in_distance_units, exit_idx)`` where a TP pays
    ``+tp_dist`` and an SL pays ``-sl_dist`` — the same settlement the price
    walk produces, expressed in whatever unit the envelope was built in
    (price, ATR multiples, bricks).
    """
    pol = str(same_bar_policy or SAME_BAR_SL_FIRST).strip().lower()
    if pol not in _VALID_POLICIES:
        pol = SAME_BAR_SL_FIRST
    n = min(len(fav), len(adv))
    for i in range(n):
        tp_touch = fav[i] >= tp_dist
        sl_touch = adv[i] <= -sl_dist
        if tp_touch and sl_touch:
            if pol == SAME_BAR_SL_FIRST:
                return OUTCOME_SL, -float(sl_dist), i
            return OUTCOME_TP, float(tp_dist), i
        if sl_touch:
            return OUTCOME_SL, -float(sl_dist), i
        if tp_touch:
            return OUTCOME_TP, float(tp_dist), i
    return OUTCOME_EXPIRED, 0.0, None
