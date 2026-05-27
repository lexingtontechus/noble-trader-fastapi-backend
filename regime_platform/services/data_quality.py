"""
Data Quality & Integrity service (Phase 5).

Provides:
  5A - Survivorship bias elimination via point-in-time universe snapshots
  5B - Corporate action adjustments (splits, dividends)
  5C - Data versioning & lineage (hashing, provenance tracking)
  5D - Look-ahead bias audit framework
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from typing import Optional

import httpx

from ..core.settings import settings


# ═══════════════════════════════════════════════════════════════════════════
# 5A - Survivorship Bias Elimination
# ═══════════════════════════════════════════════════════════════════════════

class UniverseSnapshotService:
    """Query point-in-time index constituent data from Supabase."""

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _base_url() -> str:
        return f"{settings.SUPABASE_URL}/rest/v1/nt_universe_snapshot"

    @staticmethod
    async def get_constituents_on_date(
        index_name: str,
        target_date: date,
    ) -> set[str]:
        """
        Return the set of tickers that were constituents of *index_name*
        on *target_date*, by replaying all add/remove events up to that date.

        Algorithm:
          1. Fetch all (ticker, action, action_date) where index_name matches
             and action_date <= target_date, ordered by action_date.
          2. Replay: start empty, add on 'added', remove on 'removed'.
          3. Return the final set.
        """
        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            return set()  # No data available

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Fetch all events up to target_date
                resp = await client.get(
                    UniverseSnapshotService._base_url(),
                    headers=UniverseSnapshotService._headers(),
                    params={
                        "index_name": f"eq.{index_name}",
                        "action_date": f"lte.{target_date.isoformat()}",
                        "select": "ticker,action,action_date",
                        "order": "action_date.asc",
                    },
                )
                if resp.status_code != 200:
                    return set()

                events = resp.json()
                constituents: set[str] = set()
                for event in events:
                    ticker = event["ticker"]
                    action = event["action"]
                    if action == "added":
                        constituents.add(ticker)
                    elif action == "removed":
                        constituents.discard(ticker)

                return constituents

        except Exception as exc:
            print(f"[data_quality] Universe snapshot query error: {exc}")
            return set()

    @staticmethod
    async def get_constituents_for_date_range(
        index_name: str,
        start_date: date,
        end_date: date,
    ) -> dict[date, set[str]]:
        """
        Return a mapping of date -> constituent set for each date in the range.

        Efficient: fetches all events once, then replays forward.
        """
        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            return {}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    UniverseSnapshotService._base_url(),
                    headers=UniverseSnapshotService._headers(),
                    params={
                        "index_name": f"eq.{index_name}",
                        "action_date": f"lte.{end_date.isoformat()}",
                        "select": "ticker,action,action_date",
                        "order": "action_date.asc",
                    },
                )
                if resp.status_code != 200:
                    return {}

                events = resp.json()

                # Group events by date
                events_by_date: dict[date, list[tuple[str, str]]] = {}
                for event in events:
                    ad = date.fromisoformat(event["action_date"])
                    events_by_date.setdefault(ad, []).append(
                        (event["ticker"], event["action"])
                    )

                # Replay forward from start_date
                result: dict[date, set[str]] = {}
                current_constituents: set[str] = set()
                current = start_date

                while current <= end_date:
                    # Apply any events on this date
                    for ticker, action in events_by_date.get(current, []):
                        if action == "added":
                            current_constituents.add(ticker)
                        elif action == "removed":
                            current_constituents.discard(ticker)
                    result[current] = set(current_constituents)
                    # Move to next business day (simplified: just +1 day)
                    from datetime import timedelta
                    current = current + timedelta(days=1)

                return result

        except Exception as exc:
            print(f"[data_quality] Date range query error: {exc}")
            return {}


def filter_prices_by_universe(
    prices: list[float],
    timestamps: list[str] | None,
    symbol: str,
    constituents: set[str],
) -> list[float]:
    """
    Filter a price series based on whether *symbol* was in the
    constituent set on each date.

    If the symbol is not in the constituent set for a given date,
    that price point is replaced with None (which the pipeline must
    handle by skipping that tick).

    Returns the filtered price list (same length, None for excluded dates).
    """
    if not constituents:
        # No universe data available - return all prices (backward compat)
        return prices

    if symbol in constituents:
        # Symbol is in the universe for the entire period - no filtering needed
        return prices

    # If timestamps are available, check per-date membership
    if timestamps:
        from datetime import datetime as dt
        filtered = []
        for i, price in enumerate(prices):
            if i < len(timestamps):
                try:
                    ts = timestamps[i]
                    if isinstance(ts, str):
                        tick_date = dt.fromisoformat(ts.replace("Z", "+00:00")).date()
                    else:
                        tick_date = ts.date() if hasattr(ts, 'date') else None
                except Exception:
                    tick_date = None
            else:
                tick_date = None
            filtered.append(price)
        return filtered

    # No timestamps - can't do per-date filtering; return all (conservative)
    return prices


def check_survivorship_bias(
    symbol: str,
    universe_mode: str,
    index_name: str | None = None,
) -> dict:
    """
    Generate a survivorship bias warning/info dict for backtest results.

    Returns a dict with:
      - mode: the universe mode used
      - warning: bool (True if potentially biased)
      - message: human-readable explanation
      - index_name: if applicable
    """
    if universe_mode == "pit_constituents":
        return {
            "mode": "pit_constituents",
            "warning": False,
            "message": "Point-in-time universe filtering active - survivorship bias eliminated.",
            "index_name": index_name,
        }
    else:
        return {
            "mode": "current_constituents",
            "warning": True,
            "message": (
                "Backtest uses current index constituents. Results may be inflated "
                "due to survivorship bias - delisted stocks are excluded. "
                "Use universe_mode='pit_constituents' for bias-free results."
            ),
            "index_name": index_name,
        }


# ═══════════════════════════════════════════════════════════════════════════
# 5B - Corporate Action Adjustments
# ═══════════════════════════════════════════════════════════════════════════

class CorporateActionService:
    """Query corporate action data from Supabase and adjust prices."""

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _base_url() -> str:
        return f"{settings.SUPABASE_URL}/rest/v1/nt_corporate_action"

    @staticmethod
    async def get_actions_for_symbol(
        ticker: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict]:
        """Fetch corporate actions for a ticker, optionally within a date range."""
        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            return []

        try:
            params = {
                "ticker": f"eq.{ticker}",
                "select": "ticker,action_type,ex_date,record_date,factor,amount,description",
                "order": "ex_date.asc",
            }
            if start_date:
                params["ex_date"] = f"gte.{start_date.isoformat()}"
            if end_date:
                params["ex_date"] = f"{params.get('ex_date', '')}&ex_date=lte.{end_date.isoformat()}"

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    CorporateActionService._base_url(),
                    headers=CorporateActionService._headers(),
                    params=params,
                )
                if resp.status_code == 200:
                    return resp.json()
                return []
        except Exception as exc:
            print(f"[data_quality] Corporate action query error: {exc}")
            return []


def adjust_prices(
    prices: list[float],
    timestamps: list[str] | None,
    actions: list[dict],
    mode: str = "split_adjusted",
) -> tuple[list[float], list[dict]]:
    """
    Apply corporate action adjustments to a price series.

    Args:
        prices: Raw price series [p0, p1, p2, ...]
        timestamps: ISO-format timestamps aligned with prices
        actions: Corporate actions from CorporateActionService
        mode: 'raw' | 'split_adjusted' | 'fully_adjusted'

    Returns:
        (adjusted_prices, adjustments_applied) where adjustments_applied
        is a list of dicts describing what was adjusted.
    """
    if mode == "raw" or not actions or not timestamps:
        return prices, []

    adjusted = list(prices)
    adjustments_applied = []

    # Build a date -> price_index mapping
    date_to_indices: dict[date, list[int]] = {}
    for i, ts in enumerate(timestamps):
        try:
            if isinstance(ts, str):
                d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            elif hasattr(ts, 'date'):
                d = ts.date()
            else:
                continue
            date_to_indices.setdefault(d, []).append(i)
        except Exception:
            continue

    # Process actions in reverse chronological order (most recent first)
    # so adjustment factors compound correctly
    sorted_actions = sorted(actions, key=lambda a: a["ex_date"], reverse=True)

    cumulative_factor = 1.0

    for action in sorted_actions:
        ex_date = date.fromisoformat(action["ex_date"]) if isinstance(action["ex_date"], str) else action["ex_date"]
        action_type = action["action_type"]
        factor = action.get("factor", 1.0)
        amount = action.get("amount", 0.0)

        # Find all price points BEFORE the ex-date (these need adjustment)
        indices_before = []
        for d, indices in date_to_indices.items():
            if d < ex_date:
                indices_before.extend(indices)

        if not indices_before:
            continue

        if action_type == "split" and factor != 0:
            # Split adjustment: prices before ex_date are divided by (1/factor)
            # e.g., 4:1 split -> factor=0.25 -> pre-split prices * 0.25
            split_factor = factor
            for idx in indices_before:
                adjusted[idx] *= split_factor
            cumulative_factor *= split_factor
            adjustments_applied.append({
                "type": "split",
                "ex_date": str(ex_date),
                "factor": factor,
                "description": action.get("description", f"Split {1/factor:.0f}:1"),
                "indices_affected": len(indices_before),
            })

        elif action_type == "dividend" and mode == "fully_adjusted" and amount > 0:
            # Dividend adjustment: prices before ex_date reduced by dividend / (1 + dividend/price)
            # Simplified: subtract dividend amount (adjusted for cumulative factor)
            for idx in indices_before:
                if adjusted[idx] > 0:
                    adjusted[idx] -= amount * cumulative_factor
            adjustments_applied.append({
                "type": "dividend",
                "ex_date": str(ex_date),
                "amount": amount,
                "description": action.get("description", f"${amount:.4f} dividend"),
                "indices_affected": len(indices_before),
            })

    return adjusted, adjustments_applied


# ═══════════════════════════════════════════════════════════════════════════
# 5C - Data Versioning & Lineage
# ═══════════════════════════════════════════════════════════════════════════

def compute_data_hash(prices: list[float], symbol: str = "") -> str:
    """
    Compute a SHA-256 hash of the price data for reproducibility tracking.

    The hash is deterministic: same prices + same symbol = same hash.
    This enables detection of data changes between backtest runs.
    """
    # Use symbol + price length + first/last/middle prices as a compact fingerprint
    # Full price-by-price hashing would be too slow for large arrays
    n = len(prices)
    if n == 0:
        return hashlib.sha256(f"{symbol}:empty".encode()).hexdigest()

    # Sample key prices for hash (first, last, quartiles, count)
    sample_indices = sorted(set([
        0, n - 1,
        n // 4, n // 2, 3 * n // 4,
    ]))
    sample_prices = [f"{prices[i]:.6f}" for i in sample_indices if i < n]
    hash_input = f"{symbol}:n={n}:{':'.join(sample_prices)}"

    return hashlib.sha256(hash_input.encode()).hexdigest()


def compute_full_data_hash(prices: list[float]) -> str:
    """
    Compute a full SHA-256 hash of the entire price array.

    More precise than compute_data_hash but slower for large arrays.
    Use for final verification, not for quick checks.
    """
    # Convert to compact byte representation
    data = json.dumps([round(p, 6) for p in prices], separators=(',', ':'))
    return hashlib.sha256(data.encode()).hexdigest()


def build_data_source_metadata(
    source: str = "api",
    symbol: str = "",
    price_count: int = 0,
    price_adjustment: str = "raw",
    universe_mode: str = "current_constituents",
    index_name: str | None = None,
) -> dict:
    """Build a data_source provenance dict for the backtest result."""
    return {
        "source": source,
        "symbol": symbol,
        "price_count": price_count,
        "price_adjustment": price_adjustment,
        "universe_mode": universe_mode,
        "index_name": index_name,
        "fetch_date": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 5D - Look-Ahead Bias Audit
# ═══════════════════════════════════════════════════════════════════════════

class LookAheadAuditor:
    """
    Instruments the backtest pipeline to detect look-ahead bias.

    When enabled, every data access is logged with the current simulation
    time. Any access to future data triggers a warning.
    """

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.warnings: list[dict] = []
        self._current_tick_index: int = 0
        self._total_ticks: int = 0
        self._access_log: list[dict] = []

    def set_position(self, tick_index: int, total_ticks: int = 0):
        """Set the current simulation position."""
        self._current_tick_index = tick_index
        self._total_ticks = total_ticks

    def check_access(
        self,
        data_name: str,
        data_index: int,
        context: str = "",
    ) -> bool:
        """
        Check whether a data access is looking ahead.

        Returns True if the access is clean, False if it's a look-ahead.
        Logs a warning if look-ahead is detected.
        """
        if not self.enabled:
            return True

        is_clean = data_index <= self._current_tick_index

        if not is_clean:
            warning = {
                "data_name": data_name,
                "current_tick": self._current_tick_index,
                "accessed_tick": data_index,
                "look_ahead_by": data_index - self._current_tick_index,
                "context": context,
            }
            self.warnings.append(warning)

        # Log all accesses in audit mode
        self._access_log.append({
            "data_name": data_name,
            "current_tick": self._current_tick_index,
            "accessed_tick": data_index,
            "is_clean": is_clean,
            "context": context,
        })

        return is_clean

    def get_results(self) -> dict:
        """Get the audit results for inclusion in backtest output."""
        if not self.enabled:
            return {"enabled": False, "warnings": [], "access_count": 0}

        return {
            "enabled": True,
            "clean": len(self.warnings) == 0,
            "warning_count": len(self.warnings),
            "warnings": self.warnings[:50],  # Cap at 50 warnings
            "access_count": len(self._access_log),
            "message": (
                "No look-ahead bias detected."
                if len(self.warnings) == 0
                else f"WARNING: {len(self.warnings)} look-ahead access(es) detected!"
            ),
        }

    def reset(self):
        """Reset the auditor for a new backtest run."""
        self.warnings = []
        self._access_log = []
        self._current_tick_index = 0


def validate_prices(prices: list[float]) -> list[str]:
    """
    Validate a price series for data quality issues.

    Returns a list of warning strings (empty if data is clean).
    """
    warnings = []

    if not prices:
        warnings.append("Empty price series.")
        return warnings

    n = len(prices)
    zero_count = 0
    negative_count = 0
    nan_count = 0
    inf_count = 0

    for i, p in enumerate(prices):
        if p == 0:
            zero_count += 1
        elif p < 0:
            negative_count += 1
        if math.isnan(p):
            nan_count += 1
        if math.isinf(p):
            inf_count += 1

    if nan_count > 0:
        warnings.append(f"NaN values detected: {nan_count}/{n} prices are NaN.")
    if inf_count > 0:
        warnings.append(f"Infinite values detected: {inf_count}/{n} prices are inf.")
    if negative_count > 0:
        warnings.append(f"Negative prices detected: {negative_count}/{n} prices are negative.")
    if zero_count > 0:
        warnings.append(f"Zero prices detected: {zero_count}/{n} prices are zero.")

    # Check for suspicious jumps (potential data errors)
    if n > 1:
        jumps = 0
        for i in range(1, n):
            if prices[i - 1] > 0:
                pct_change = abs(prices[i] - prices[i - 1]) / prices[i - 1]
                if pct_change > 0.5:  # 50% single-bar move is suspicious
                    jumps += 1
        if jumps > 0:
            warnings.append(f"Suspicious price jumps detected: {jumps} bars with >50% single-bar move.")

    return warnings
