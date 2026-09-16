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


def _as_float_list(bars: Sequence[Any] | None, key: str) -> list[float] | None:
    if bars is None:
        return None
    out: list[float] = []
    for b in bars:
        if isinstance(b, Mapping):
            v = b.get(key)
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
    """
    if not bars:
        return [], None, None
    if not isinstance(bars[0], Mapping):
        return [float(x) for x in bars], None, None
    closes = _as_float_list(bars, "close")
    highs = _as_float_list(bars, "high")
    lows = _as_float_list(bars, "low")
    if closes is None:
        raise ValueError("settlement_contract: OHLC bars require a 'close' field")
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
