"""YAML-backed brick parameter loader (v5).

Replaces the Redis ``trading:config:{symbol}`` hash as the source of truth
for ``brick_size``, ``sl_bricks``, ``tp_bricks`` per symbol.

Heavy sweep (weekly, weekend 20:00 UTC) rewrites the YAML file with ATR-scaled
optimised values. Light sweep (every 15 min) reads from the YAML file — no
Redis round-trip for config.

The file lives at ``config/brick_params.yaml`` (relative to the backend
repo root). Override the path via the ``BRICK_PARAMS_YAML`` env var.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Tuple
from functools import lru_cache

logger = logging.getLogger("regime.config.brick_params")

# Default path — overridable via env var
_DEFAULT_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "brick_params.yaml"


def _yaml_path() -> Path:
    """Resolve the YAML path from env var or default."""
    env_path = os.getenv("BRICK_PARAMS_YAML")
    if env_path:
        return Path(env_path)
    return _DEFAULT_YAML_PATH


@lru_cache(maxsize=1)
def _load_yaml_cached() -> Dict:
    """Load and cache the YAML config. Cache is invalidated by ``reload()``
    or by setting ``BRICK_PARAMS_YAML_NOCACHE=1``."""
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed — brick params will use defaults")
        return {"defaults": {"brick_size": 0.5, "sl_bricks": 3, "tp_bricks": 6}, "symbols": {}}

    path = _yaml_path()
    if not path.exists():
        logger.warning(f"Brick params YAML not found at {path} — using defaults")
        return {"defaults": {"brick_size": 0.5, "sl_bricks": 3, "tp_bricks": 6}, "symbols": {}}

    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        if "defaults" not in data:
            data["defaults"] = {"brick_size": 0.5, "sl_bricks": 3, "tp_bricks": 6}
        if "symbols" not in data:
            data["symbols"] = {}
        return data
    except Exception as exc:
        logger.error(f"Failed to load brick params YAML ({exc}) — using defaults")
        return {"defaults": {"brick_size": 0.5, "sl_bricks": 3, "tp_bricks": 6}, "symbols": {}}


def reload() -> None:
    """Force a re-read of the YAML file on the next ``load_brick_params`` call.

    Called by the heavy sweep after it rewrites the YAML file.
    """
    _load_yaml_cached.cache_clear()
    logger.info("Brick params YAML cache invalidated — next read will re-load file")


# ─── Store authority (2026-10-09, Part B single source of truth) ───────────
#
# worklog 20261009_DEPENDENCY_MAP_AND_HARMONIZATION_THESIS.md Part B: the
# LOCAL param store is the geometry AUTHORITY; brick_params.yaml is a derived
# CACHE. The YAML stays written (renko_retention's prune key and
# renko/router's Discord embed read brick_size from it) but it is no longer
# the thing the sweep treats as truth.
#
# The store already carries the complete triple in the `brick_state` tier
# (written by the heavy sweep alongside the YAML), so no schema change and no
# new field on the ATR pick are needed.

def load_from_store(symbol: str) -> Tuple[float, int, int] | None:
    """Read ``(brick_size, sl_bricks, tp_bricks)`` from the LOCAL param store.

    Local-only, no network. Returns ``None`` when the store has no usable
    entry for the symbol — the caller MUST fall back to the YAML (fail-open;
    a cold store must never leave the sweep without geometry).
    """
    try:
        from regime_platform.core import param_store

        d = param_store.read_dict(symbol, param_store.TIER_BRICK_STATE, "latest")
        if not d:
            return None
        bs, sl, tp = d.get("brick_size"), d.get("sl_bricks"), d.get("tp_bricks")
        if bs is None or sl is None or tp is None:
            return None
        return (float(bs), int(sl), int(tp))
    except Exception as exc:
        logger.debug("load_from_store %s: %s", symbol, exc)
        return None


def load_brick_params_authoritative(symbol: str) -> Tuple[float, int, int]:
    """The sweep-facing read: store first (authority), YAML second (cache).

    Gated by ``brick_params_source`` (config/feature_flags.yaml):
      * ``yaml``  (default) — the historical read, unchanged.
      * ``store`` — read the store; fall back to the YAML when cold.

    ``store`` mode is the Part B end state: one authority, one derived cache.
    It ships defaulted to ``yaml`` so the flip is one config edit with no
    restart, and a cold store is never fatal.
    """
    try:
        from regime_platform.core.feature_flags import get_brick_params_source

        mode = get_brick_params_source()
    except Exception:
        mode = "yaml"

    if mode == "store":
        stored = load_from_store(symbol)
        if stored is not None:
            return stored
        logger.debug(
            "brick_params_source=store but no stored geometry for %s — "
            "falling back to YAML", symbol,
        )
    return load_brick_params(symbol)


def load_brick_params(symbol: str) -> Tuple[float, int, int]:
    """Load (brick_size, sl_bricks, tp_bricks) for a symbol.

    Falls back to ``defaults`` if the symbol is not listed in the ``symbols``
    section. Falls back to (0.5, 3, 6) if the YAML is missing or malformed.

    Args:
        symbol: Symbol name (e.g., "BTC", "AAPL", "EURUSD").

    Returns:
        Tuple of (brick_size, sl_bricks, tp_bricks).
    """
    # Bypass cache when explicitly disabled (testing/debug)
    if os.getenv("BRICK_PARAMS_YAML_NOCACHE") == "1":
        _load_yaml_cached.cache_clear()

    data = _load_yaml_cached()
    defaults = data.get("defaults", {})
    symbols = data.get("symbols", {})

    # Per-symbol overrides take precedence
    sym_config = symbols.get(symbol, {})

    try:
        brick_size = float(sym_config.get("brick_size", defaults.get("brick_size", 0.5)))
        sl_bricks = int(sym_config.get("sl_bricks", defaults.get("sl_bricks", 3)))
        tp_bricks = int(sym_config.get("tp_bricks", defaults.get("tp_bricks", 6)))
        return (brick_size, sl_bricks, tp_bricks)
    except (TypeError, ValueError) as exc:
        logger.warning(
            f"Malformed brick params for {symbol} ({exc}) — using hardcoded defaults"
        )
        return (0.5, 3, 6)


def write_brick_params(updates: Dict[str, Dict[str, float | int]]) -> None:
    """Write per-symbol brick param updates back to the YAML file.

    Called by the heavy sweep after ATR-scaled optimisation. Preserves the
    ``defaults`` section and any symbols not in ``updates``.

    Args:
        updates: Dict mapping symbol name → {brick_size, sl_bricks, tp_bricks}.
    """
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed — cannot write brick params YAML")
        return

    path = _yaml_path()
    data = _load_yaml_cached().copy()
    data["symbols"] = dict(data.get("symbols", {}))
    for sym, params in updates.items():
        data["symbols"][sym] = dict(params)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        # Invalidate the cache so subsequent reads pick up the new values
        reload()
        logger.info(f"Wrote {len(updates)} symbol brick param updates to {path}")
    except Exception as exc:
        logger.error(f"Failed to write brick params YAML ({exc})")


# ─── nt_symbol sync ──────────────────────────────────────────────────
# The YAML ``symbols:`` section must stay aligned with nt_symbol.active=true.
# This sync is called at orchestrator startup (after fetch_active_symbols)
# and on each _refresh_symbols cycle (every 5 min). It ensures:
#
#   1. Every active nt_symbol has a YAML entry (defaults used if missing)
#   2. Inactive nt_symbols are pruned from YAML (orphan cleanup)
#   3. Asset-class-aware defaults are applied when seeding new entries
#
# Without this sync, the YAML drifts from nt_symbol whenever the admin
# toggles active=true/false in the DB. The heavy sweep is the ONLY writer
# of optimised brick params; the sync only seeds sensible defaults.

# Asset-class sensible defaults for new symbols (heavy sweep will optimise).
# These match the seed values in config/brick_params.yaml's existing entries.
_ASSET_CLASS_DEFAULTS: Dict[str, Dict[str, float | int]] = {
    "crypto":     {"brick_size": 1.0,   "sl_bricks": 3, "tp_bricks": 6},
    "equity":     {"brick_size": 1.0,   "sl_bricks": 3, "tp_bricks": 6},
    "stocks":     {"brick_size": 1.0,   "sl_bricks": 3, "tp_bricks": 6},
    "etf":        {"brick_size": 1.0,   "sl_bricks": 3, "tp_bricks": 6},
    "forex":      {"brick_size": 0.001, "sl_bricks": 3, "tp_bricks": 6},
    "commodities":{"brick_size": 1.0,   "sl_bricks": 3, "tp_bricks": 6},
}


def sync_yaml_to_nt_symbol(active_symbols: list[dict]) -> dict[str, int]:
    """Synchronise the YAML ``symbols:`` section with nt_symbol.active=true.

    Called by the sweep orchestrator after ``fetch_active_symbols()``. Ensures
    every active symbol in the DB has a corresponding entry in the YAML file,
    and prunes YAML entries for symbols that are no longer active.

    The heavy sweep (weekly) is the only writer of OPTIMISED brick params.
    This sync only seeds sensible defaults for newly-activated symbols so
    that the light sweep has something to read before the next heavy sweep
    runs. Existing per-symbol overrides are preserved (not overwritten).

    Args:
        active_symbols: List of nt_symbol rows (as returned by
            ``fetch_active_symbols()``). Each row must have at least
            ``symbol`` and optionally ``asset_class``.

    Returns:
        Dict with ``added``, ``pruned``, ``unchanged`` counts for logging.
    """
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed — cannot sync YAML to nt_symbol")
        return {"added": 0, "pruned": 0, "unchanged": 0}

    path = _yaml_path()
    data = _load_yaml_cached().copy()
    yaml_symbols = dict(data.get("symbols", {}))

    # Build set of active symbol names from DB
    active_set = {r.get("symbol") for r in active_symbols if r.get("symbol")}

    added = 0
    for row in active_symbols:
        sym = row.get("symbol")
        if not sym:
            continue
        if sym not in yaml_symbols:
            # Seed with asset-class default
            asset_class = (row.get("asset_class") or "").lower()
            defaults = _ASSET_CLASS_DEFAULTS.get(
                asset_class,
                {"brick_size": 0.5, "sl_bricks": 3, "tp_bricks": 6},
            )
            yaml_symbols[sym] = dict(defaults)
            added += 1
            logger.info(
                f"YAML sync: added {sym} (asset_class={asset_class}) "
                f"with defaults {defaults} — heavy sweep will optimise"
            )

    # Prune YAML entries for symbols no longer active in DB
    pruned = 0
    yaml_only = set(yaml_symbols.keys()) - active_set
    for sym in list(yaml_only):
        # Only prune if there's an actual nt_symbol row marked inactive.
        # If the symbol is unknown to nt_symbol entirely, keep it (could
        # be a local-only dev symbol that bypasses the DB).
        # We can't query the DB from here (sync function), so we prune
        # only when active_symbols is non-empty (i.e., the DB is reachable).
        if active_symbols:
            del yaml_symbols[sym]
            pruned += 1
            logger.info(f"YAML sync: pruned {sym} (no longer active in nt_symbol)")

    unchanged = len(yaml_symbols) - added

    if added > 0 or pruned > 0:
        data["symbols"] = yaml_symbols
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            reload()
            logger.info(
                f"YAML sync complete: {added} added, {pruned} pruned, "
                f"{unchanged} unchanged ({len(yaml_symbols)} total)"
            )
        except Exception as exc:
            logger.error(f"YAML sync write failed ({exc})")
    else:
        logger.debug(
            f"YAML sync: no changes ({unchanged} symbols already aligned)"
        )

    return {"added": added, "pruned": pruned, "unchanged": unchanged}
