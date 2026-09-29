"""
Dynamic ATR — TP/SL picked by realised expectancy on the platform's own signals
═══════════════════════════════════════════════════════════════════════════════

worklog/20260918_TIERED_ATR_GEOMETRY_SCOPE.md; evidence: scope §14.5
(worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md) and
worklog/kairos/reports/20260926_geometry_replay.md.

``dynamic_atr_mode`` (off | shadow | live) is the platform's geometry lever.
On each heavy sweep it chooses the bracket (TP and SL distance in ATR units)
for the symbol's asset class:

  1. Load the class's signals from the last ``lookback_days`` whose 48-bar
     window has finished (nt_signal_sim, one per symbol x direction x hour).
  2. Re-walk each one under every TP x SL candidate with the live resolver's
     rule (1h closes, first touch, 48-bar expiry, TP first on a tie).
  3. Keep candidates with TP/SL >= min_rr, expiry share <= ``max_expiry`` and
     positive expectancy (net of ``cost_atr``) in BOTH halves of the window.
  4. Pick the highest expectancy. Nothing qualifies -> keep the heavy pick.

The pick is expressed in bricks with ``sl_bricks`` fixed, so
brick = sl_atr x ATR / sl_bricks and tp_bricks = round(tp_atr / sl_atr x sl_bricks).

Rolling walk-forward (fit on the trailing 10 days, apply the next day,
08-26 -> 09-24, 4,647 signals, cost 0.1 ATR): recorded geometry -1.55 ATR per
signal, this selector +0.82; the old band-limited pick (TP <= 2 ATR) -1.50.

Also here: the ATR-anchored evidence promote helpers used by the heavy
sweep's candidate pool (``should_promote_atr_candidates``,
``compose_deadline_candidate_pool``, ``fetch_bucket_long_lag``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np

from regime_platform.sweep_orchestrator.geometry_risk import GEOMETRY_RISK_CAP

logger = logging.getLogger("regime.sweep.tiered_atr")

EXPIRY_BARS = 48
DEFAULT_RISK_CAP = GEOMETRY_RISK_CAP


def asset_class_key(asset_class: str | None) -> str:
    if not asset_class:
        return "default"
    a = str(asset_class).strip().lower()
    if "crypto" in a or a in ("btc", "coin"):
        return "crypto"
    if "forex" in a or "fx" in a or a in ("currency",):
        return "forex"
    if "metal" in a or a in ("gold", "silver", "xau", "xag"):
        return "metal"
    if "commod" in a:
        return "commodity"
    if "etf" in a:
        return "etf"
    if "index" in a or a in ("us30", "us500", "ndx"):
        return "index"
    if "equity" in a or "stock" in a or "share" in a:
        return "equity"
    return "default"


# ─── Replay walk (same rule as the live resolver) ──────────────────────────

def walk(paths: np.ndarray, tp_atr: float, sl_atr: float) -> tuple[np.ndarray, np.ndarray]:
    """First-passage over direction-signed, ATR-scaled close paths.

    ``paths`` is (n, 48): (close - entry) * dir / atr. Returns
    (realised pnl in ATR at the triggering close, expired mask). TP first on
    a same-bar tie; expired trades are marked to market at bar 48.
    """
    n = len(paths)
    hit_tp = paths >= tp_atr
    hit_sl = paths <= -sl_atr
    ti = np.where(hit_tp.any(1), hit_tp.argmax(1), EXPIRY_BARS)
    si = np.where(hit_sl.any(1), hit_sl.argmax(1), EXPIRY_BARS)
    tp_first = (ti < EXPIRY_BARS) & (ti <= si)
    sl_first = ~tp_first & (si < EXPIRY_BARS)
    exit_bar = np.where(tp_first, ti, np.where(sl_first, si, EXPIRY_BARS - 1))
    pnl = paths[np.arange(n), exit_bar]
    return pnl, ~(tp_first | sl_first)


@dataclass(frozen=True)
class DynamicAtrPick:
    tp_atr: float
    sl_atr: float
    exp_atr: float          # mean realised pnl per signal, ATR, net of cost
    exp_h1: float
    exp_h2: float
    expiry: float           # share of signals expired at 48 bars
    n: int

    def to_bricks(self, atr: float, sl_bricks: int) -> tuple[float, int, int]:
        brick = self.sl_atr * float(atr) / sl_bricks
        tp_bricks = max(1, int(round(self.tp_atr / self.sl_atr * sl_bricks)))
        return brick, int(sl_bricks), tp_bricks

    def as_log(self) -> str:
        return (f"tp={self.tp_atr:g}ATR sl={self.sl_atr:g}ATR exp={self.exp_atr:+.3f} "
                f"(h1 {self.exp_h1:+.3f} h2 {self.exp_h2:+.3f}) expiry={self.expiry:.0%} n={self.n}")


def select_tp_sl(
    paths: np.ndarray,
    *,
    tp_grid: list[float],
    sl_grid: list[float],
    min_rr: float,
    max_expiry: float,
    cost_atr: float,
    min_signals: int,
    tp_atr_min: float | None = None,
    tp_atr_max: float | None = None,
    sl_atr_min: float | None = None,
    sl_atr_max: float | None = None,
) -> Optional[DynamicAtrPick]:
    """Best TP/SL by expectancy; must be positive in both halves (rows are time-ordered).

    Optional band clamps (2026-09-29 geometry_rework): drop candidates outside
    [tp_atr_min, tp_atr_max] x [sl_atr_min, sl_atr_max] even if the grid lists
    them — the 10d walk otherwise prefers a wide TP outside the replay's
    best region (TP 9-17 / SL 0.5-2).
    """
    n = len(paths)
    if n < min_signals:
        return None
    mid = n // 2
    best: Optional[DynamicAtrPick] = None
    for tp in tp_grid:
        for sl in sl_grid:
            tp_f, sl_f = float(tp), float(sl)
            if sl_f <= 0 or tp_f / sl_f < min_rr:
                continue
            if tp_atr_min is not None and tp_f < tp_atr_min:
                continue
            if tp_atr_max is not None and tp_f > tp_atr_max:
                continue
            if sl_atr_min is not None and sl_f < sl_atr_min:
                continue
            if sl_atr_max is not None and sl_f > sl_atr_max:
                continue
            pnl, expired = walk(paths, tp_f, sl_f)
            if expired.mean() > max_expiry:
                continue
            net = pnl - cost_atr
            e1, e2 = float(net[:mid].mean()), float(net[mid:].mean())
            if e1 <= 0 or e2 <= 0:
                continue
            e = float(net.mean())
            if best is None or e > best.exp_atr:
                best = DynamicAtrPick(tp_f, sl_f, e, e1, e2, float(expired.mean()), n)
    return best


def build_paths(signals: list[dict], bars: dict[str, list[dict]]) -> np.ndarray:
    """(n, 48) ATR-scaled paths for signals with a full 48-bar window, time-ordered."""
    tcache = {s: np.array([float(b["t"]) for b in rows], dtype="float64") for s, rows in bars.items()}
    ccache = {s: np.array([float(b["c"]) for b in rows], dtype="float64") for s, rows in bars.items()}
    out = []
    for r in sorted(signals, key=lambda x: x["_ts"]):
        t = tcache.get(r["symbol"])
        if t is None or not len(t):
            continue
        j = int(np.searchsorted(t, r["_ts"], side="right")) - 1   # bar in progress at entry
        if j < 0 or j + EXPIRY_BARS > len(t):
            continue
        d = 1.0 if r["signal"] == "buy" else -1.0
        out.append((ccache[r["symbol"]][j:j + EXPIRY_BARS] - r["entry_price"]) * d / r["atr"])
    return np.vstack(out) if out else np.empty((0, EXPIRY_BARS))


# ─── Production entry point ────────────────────────────────────────────────

_PICK_CACHE: dict[str, tuple[float, Optional[DynamicAtrPick]]] = {}
_PICK_TTL_S = 6 * 3600


async def _load_class_signals(symbols: list[str], since: datetime, until: datetime) -> list[dict]:
    import httpx

    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    rows: list[dict] = []
    off = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(_url("nt_signal_sim"), headers=_headers(), params={
                "select": "symbol,signal,ts,entry_price,atr",
                "source": "eq.sweep-light",
                "signal": "in.(buy,sell)",
                "symbol": f"in.({','.join(symbols)})",
                "and": f"(ts.gte.{since.isoformat()},ts.lt.{until.isoformat()})",
                "order": "ts.asc",
                "limit": "1000",
                "offset": str(off),
            })
            resp.raise_for_status()
            batch = resp.json() or []
            rows += batch
            off += len(batch)
            if len(batch) < 1000:
                break
    out, seen = [], set()
    for r in rows:
        try:
            atr = float(r.get("atr") or 0)
            entry = float(r["entry_price"])
            ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, KeyError):
            continue
        if atr <= 0:
            continue
        key = (r["symbol"], r["signal"], int(ts // 3600))   # 15-min sweeps repeat the call
        if key in seen:
            continue
        seen.add(key)
        out.append({"symbol": r["symbol"], "signal": r["signal"], "entry_price": entry,
                    "atr": atr, "_ts": ts})
    return out


async def dynamic_atr_pick(
    asset_class: str | None,
    *,
    cfg: dict[str, Any],
    min_rr: float,
) -> Optional[DynamicAtrPick]:
    """TP/SL for this asset class from its recent signals; None -> keep heavy pick.

    Cached per class for 6h so a daily heavy group walks each class once.
    """
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    ac = asset_class_key(asset_class)
    hit = _PICK_CACHE.get(ac)
    if hit and time.time() - hit[0] < _PICK_TTL_S:
        return hit[1]

    peers = await fetch_active_symbols()
    members = [p for p in peers if asset_class_key(p.get("asset_class")) == ac and p.get("tradingview_symbol")]
    if not members:
        return None
    lookback = int(cfg["lookback_days"])
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=lookback)
    signals = await _load_class_signals([m["symbol"] for m in members], since, until)
    if len(signals) < int(cfg["min_signals"]):
        _PICK_CACHE[ac] = (time.time(), None)
        return None
    bars: dict[str, list[dict]] = {}
    await fetch_tradingview_batch(
        [{"symbol": m["symbol"], "tradingview_symbol": m["tradingview_symbol"]} for m in members],
        hours=24 * (lookback + 5), interval="1h", ohlcv_out=bars,
    )
    bars = {s: [b for b in rows if b.get("t") is not None and b.get("c") is not None]
            for s, rows in bars.items()}
    paths = build_paths(signals, bars)
    pick = select_tp_sl(
        paths,
        tp_grid=[float(x) for x in cfg["tp_atr"]],
        sl_grid=[float(x) for x in cfg["sl_atr"]],
        min_rr=min_rr,
        max_expiry=float(cfg["max_expiry"]),
        cost_atr=float(cfg["cost_atr"]),
        min_signals=int(cfg["min_signals"]),
        # 2026-09-29 band rework — clamp to replay best region even if the
        # grid is widened later (tp=24 used to win the 10d walk).
        tp_atr_min=cfg.get("tp_atr_min"),
        tp_atr_max=cfg.get("tp_atr_max"),
        sl_atr_min=cfg.get("sl_atr_min"),
        sl_atr_max=cfg.get("sl_atr_max"),
    )
    _PICK_CACHE[ac] = (time.time(), pick)
    return pick


# ─── ATR-anchored evidence promote (heavy candidate pool) ─────────────────

def should_promote_atr_candidates(
    *,
    selection_changed: bool,
    baseline_bucket: str | None,
    widened_bucket: str | None,
    pct_expired_by_bucket: dict[str, float | None] | None,
    risk_cap: float = DEFAULT_RISK_CAP,
) -> bool:
    """P0 evidence rule: allow ATR-anchored candidates into the live pick pool.

    Promote only when shadow comparison says the pick **changed** and the
    widened bucket is strictly better on long-lag (lower pct_expired) than
    baseline — or baseline is 4+ and widened is a tighter band under cap.
    Missing metrics → no promote (fail-closed for evidence).
    """
    if not selection_changed:
        return False
    exp = pct_expired_by_bucket or {}
    b_lag = exp.get(baseline_bucket) if baseline_bucket else None
    w_lag = exp.get(widened_bucket) if widened_bucket else None
    tighter = {"<0.5", "0.5-1", "1-2", "2-4"}
    # 2026-09-29 control-symbol fix: when the pick "changed" but the bucket
    # stays the same (often stuck on 4+ for gold/USDCAD), still admit ATR
    # candidates so tighter SL bands can enter the pool — basket bucket alone
    # does not capture SL-atr profitability (see 20260929_CONTROL_GEOMETRY).
    if selection_changed and baseline_bucket is not None and baseline_bucket == widened_bucket:
        return True
    if baseline_bucket == "4+" and widened_bucket in tighter:
        if w_lag is not None and float(w_lag) <= risk_cap:
            return True
        if widened_bucket in ("0.5-1", "1-2", "<0.5") and w_lag is None:
            # Tighter ATR band with unknown lag — still prefer vs known-bad 4+
            return b_lag is None or float(b_lag) > risk_cap
    if b_lag is not None and w_lag is not None:
        return float(w_lag) < float(b_lag) and float(w_lag) <= risk_cap
    return False


def compose_deadline_candidate_pool(
    grid_results: list,
    original_brick_sizes: set,
    atr_anchored_mode: str,
    *,
    promote: bool,
) -> list:
    """P0.3: pool fed into deadline-aware scoring.

    * atr_anchored_mode == live → full grid
    * shadow + evidence promote → full grid (ATR candidates eligible)
    * shadow without promote → original brick sizes only (leak-safe)
    * off → original sizes only
    """
    if atr_anchored_mode == "live" or (atr_anchored_mode != "off" and promote):
        return list(grid_results)
    return [c for c in grid_results if getattr(c, "brick_size", None) in original_brick_sizes]


async def fetch_bucket_long_lag(symbol: str) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Load (pct_expired_by_bucket, p90_by_bucket) from reachability for risk scores."""
    import httpx

    from regime_platform.core.settings import settings
    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    exp: dict[str, float | None] = {}
    p90: dict[str, float | None] = {}
    if not settings.SUPABASE_URL:
        return exp, p90
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_bracket_reachability"),
                headers=_headers(),
                params={
                    "select": "distance_atr_bucket,pct_expired,p90_hours,n_stored",
                    "symbol": f"eq.{symbol}",
                    "signal": "eq.all",
                    "limit": "20",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
        for r in rows if isinstance(rows, list) else []:
            if int(r.get("n_stored") or 0) < 3:
                continue
            b = r.get("distance_atr_bucket")
            if not b:
                continue
            exp[b] = r.get("pct_expired")
            p90[b] = r.get("p90_hours")
    except Exception as exc:
        logger.warning("tiered_atr fetch_bucket_long_lag %s: %s", symbol, exc)
    return exp, p90
