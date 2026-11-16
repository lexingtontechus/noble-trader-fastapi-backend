#!/usr/bin/env python
"""run_crt_tv_export.py — Path B: the TDVA-basis research corpus export.

Fetches TradingView (TDVA) candles per symbol/timeframe, runs the CRT-1
engine's own setup detection + compact-path labeling (regime_edge_crt
.tv_corpus), and writes ``CRT_CONT_RESEARCH_<SYMBOL>.csv`` files in the
EXACT CRT_EV_Research_Export.mq4 v1.10 schema into ``data/crt/tv/`` — so
the CRT-0 lab can mint ``config/crt_cont.json`` WITHOUT MT4:

    python scripts/run_crt_tv_export.py
    python scripts/run_crt_tv_export.py --symbols XAUUSD,EURUSD
    python scripts/run_crt_tv_export.py --offline          # from cache only
    python scripts/run_crt_tv_export.py --no-discord

then:  python scripts/run_crt_lab.py --data-dir data/crt/tv

BASIS HONESTY (worklog Task 74/75): this corpus is the ENGINE's basis —
the same detect_setups/atr/sessions/candles ports and the same TDVA feed
crt-engine-tick trades, with ``basis=tdva_tv`` stamped in the ``#meta``
line (extra keys travel into the artifact's basis block). Costs are the
crt_tv_export.yaml per-symbol values (build-time mirror of crt_engine.yaml).
Depth honesty: TDVA returns whatever history it has (we request more than
the depth; the reconstruct_signal_history.py range=20000 precedent) — the
export REPORTS the span it got per TF; thin spans are visible, never padded.

This is offline research: no live surface, no claims writes, no MT5.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from crt.candles import aggregate_m30, closed_bars  # noqa: E402
from crt.config_loader import TF_SECONDS  # noqa: E402
from crt.sessions import SessionWindows  # noqa: E402
from regime_edge_crt import discord_notify, tv_corpus  # noqa: E402
from regime_edge_crt.config import (  # noqa: E402
    load_discord_config,
    load_tv_export_config,
    resolve_repo_relative,
)

# ── TDVA fetch (the price_fetcher conventions, big-range history) ─────────
_EPOCH_MS_THRESHOLD = 1e12


def _norm_epoch(raw) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v > _EPOCH_MS_THRESHOLD:
        v /= 1000.0
    return v


def _opt_float(raw) -> float | None:
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


def _parse_history(item: dict) -> list[dict]:
    """One batch item's history bars -> ascending closed-bar rows.

    Mirrors the platform fetcher: dict bars with time/open/max/min/close
    (or t/o/h/l/c), epoch ms-vs-s heuristic, DESCENDING -> ASCENDING,
    dedupe by t (last wins), malformed bars dropped.
    """
    data_obj = item.get("data", item)
    bars = (data_obj.get("history", []) if isinstance(data_obj, dict) else [])
    out: dict[float, dict] = {}
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        close = b.get("close", b.get("c"))
        if close is None:
            continue
        t = _norm_epoch(b.get("time", b.get("t")))
        if t is None:
            continue
        o = _opt_float(b.get("open", b.get("o")))
        h = _opt_float(b.get("max", b.get("high", b.get("h"))))
        low = _opt_float(b.get("min", b.get("low", b.get("l"))))
        if None in (o, h, low):
            continue
        out[t] = {"t": t, "o": o, "h": h, "l": low, "c": float(close),
                  "v": _opt_float(b.get("volume", b.get("v")))}
    return [out[k] for k in sorted(out)]


async def fetch_tv(symbol_cfgs: list[dict], tf_requests: dict[str, int],
                   timeout: float) -> dict[tuple[str, str], list[dict]]:
    """Fetch per (symbol, tf) history via POST /api/price/batch.

    One HTTP call per symbol carrying all its TF requests (<= 10 requests
    per call — the endpoint's documented batch limit). One bounded 429
    retry honoring Retry-After (the platform precedent, capped 10s).
    Returns {(symbol, tf): ascending bars}; failures raise CRTExportError.
    """
    import httpx

    from regime_platform.core.settings import settings

    if not settings.TRADINGVIEW_API_KEY:
        raise SystemExit(
            "TRADINGVIEW_API_KEY is not set — run on the deployment box (or "
            "export it), or use --offline with a populated cache_dir")
    headers = {"x-rapidapi-key": settings.TRADINGVIEW_API_KEY,
               "x-rapidapi-host": settings.TRADINGVIEW_API_HOST,
               "Content-Type": "application/json"}
    url = f"{settings.TRADINGVIEW_BASE_URL}/api/price/batch"
    results: dict[tuple[str, str], list[dict]] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for sc in symbol_cfgs:
            requests_body = [
                {"symbol": sc["tradingview_symbol"],
                 "timeframe": ("15" if tf == "M15" else "60"),
                 "range": int(tf_requests[tf])}
                for tf in ("M15", "H1") if tf_requests.get(tf)
            ]
            if not requests_body:
                continue
            data = None
            for attempt in range(2):
                try:
                    resp = await client.post(url, headers=headers,
                                             json={"requests": requests_body})
                    if resp.status_code == 429 and attempt == 0:
                        try:
                            wait = min(float(resp.headers.get("Retry-After", "5")),
                                       10.0)
                        except (TypeError, ValueError):
                            wait = 5.0
                        print(f"[tv-export] 429 for {sc['symbol']} — retry in {wait:.0f}s")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception as exc:  # noqa: BLE001 - loud, per-symbol
                    raise SystemExit(f"TDVA fetch failed for {sc['symbol']}: {exc!r}")
            if data is None:
                raise SystemExit(f"TDVA fetch failed for {sc['symbol']} (429 x2)")
            items = (data.get("data", {}) or {}).get("data", [])
            for item in items:
                if not isinstance(item, dict) or not item.get("success", True):
                    print(f"[tv-export] WARNING: {sc['symbol']} request returned "
                          f"success=false: {str(item)[:160]}", file=sys.stderr)
                    continue
                tv_sym = item.get("symbol", "")
                if tv_sym != sc["tradingview_symbol"]:
                    continue
                bars = _parse_history(item)
                if not bars:
                    print(f"[tv-export] WARNING: no history bars for "
                          f"{sc['symbol']} ({tv_sym})", file=sys.stderr)
                    continue
                for tf in ("M15", "H1"):
                    if tf_requests.get(tf):
                        results[(sc["symbol"], tf)] = bars
                print(f"[tv-export] {sc['symbol']} ({tv_sym}): {len(bars)} bars "
                      f"per timeframe request")
    return results


# ── cache ──────────────────────────────────────────────────────────────────
def _cache_path(cache_dir: str, symbol: str, tf: str) -> str:
    return os.path.join(cache_dir, f"{symbol}_{tf}.json")


def _write_cache(cache_dir: str, symbol: str, tf: str, bars: list[dict]) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    payload = {"symbol": symbol, "tf": tf, "fetched_at": time.time(),
               "n_bars": len(bars), "bars": bars}
    with open(_cache_path(cache_dir, symbol, tf), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _read_cache(cache_dir: str, symbol: str, tf: str) -> list[dict] | None:
    p = _cache_path(cache_dir, symbol, tf)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
        bars = doc.get("bars") or []
        return bars if isinstance(bars, list) and bars else None
    except (OSError, ValueError):
        return None


# ── main ───────────────────────────────────────────────────────────────────
def _parse_start(value: str) -> float | None:
    """'' | epoch seconds | ISO date -> epoch seconds (trim bars before)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise SystemExit(f"invalid start {value!r} (epoch or ISO date)")


def _trim(bars: list[dict], start_epoch: float | None) -> list[dict]:
    if start_epoch is None:
        return bars
    return [b for b in bars if b["t"] >= start_epoch]


def _span_months(t0: float, t1: float) -> float:
    return (t1 - t0) / (30.44 * 86400.0)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None,
                   help="export config yaml (default config/crt/crt_tv_export.yaml)")
    p.add_argument("--symbols", default=None, help="comma list filter")
    p.add_argument("--out-dir", default=None, help="override out_dir")
    p.add_argument("--cache-dir", default=None, help="override cache_dir")
    p.add_argument("--m15-range", type=int, default=None)
    p.add_argument("--h1-range", type=int, default=None)
    p.add_argument("--start", default=None, help="drop bars before epoch/ISO date")
    p.add_argument("--offline", action="store_true",
                   help="use cached bars only (no TDVA call; error if missing)")
    p.add_argument("--no-discord", action="store_true", help="no #crt signal")
    p.add_argument("--discord-config", default=None)
    a = p.parse_args()

    cfg = load_tv_export_config(a.config)
    out_dir = resolve_repo_relative(a.out_dir or cfg["out_dir"], cfg["_path"])
    cache_dir = resolve_repo_relative(a.cache_dir or cfg["cache_dir"], cfg["_path"])
    symbol_cfgs = list(cfg["symbols"])
    if a.symbols:
        keep = {s.strip().upper() for s in a.symbols.split(",") if s.strip()}
        symbol_cfgs = [s for s in symbol_cfgs
                       if str(s.get("symbol", "")).upper() in keep]
    if not symbol_cfgs:
        print("no symbols selected (check --symbols / the yaml symbols block)")
        return 2

    tfs = {tf: bool(on) for tf, on in (cfg.get("timeframes") or {}).items()}
    need_m15 = tfs.get("M15", True) or tfs.get("M30", True)
    need_h1 = tfs.get("H1", True)
    tf_requests: dict[str, int] = {}
    if need_m15:
        tf_requests["M15"] = int(a.m15_range or (cfg["fetch"].get("m15_range", 60000)))
    if need_h1:
        tf_requests["H1"] = int(a.h1_range or (cfg["fetch"].get("h1_range", 25000)))

    grid = tv_corpus.ExportGrid(
        eb_grid=tuple(float(x) for x in cfg["grid"]["entry_buf"]),
        sb_grid=tuple(float(x) for x in cfg["grid"]["sl_buf"]),
        r_grid=tuple(float(x) for x in cfg["grid"]["r"]),
        hold_grid=tuple(int(x) for x in cfg["grid"]["hold"]),
    )
    core = tv_corpus.CoreParams.from_cfg(cfg.get("core"))
    windows = SessionWindows.from_cfg(cfg.get("sessions"))
    start_epoch = _parse_start(a.start if a.start is not None else cfg.get("start", ""))

    # ── bars per (symbol, tf): fetch (with cache write) or cache-only ──────
    raw: dict[tuple[str, str], list[dict]] = {}
    if a.offline:
        missing = []
        for sc in symbol_cfgs:
            sym = str(sc["symbol"]).upper()
            if need_m15:
                bars = _read_cache(cache_dir, sym, "M15")
                if bars is None:
                    missing.append(f"{sym}/M15")
                else:
                    raw[(sym, "M15")] = bars
            if need_h1:
                bars = _read_cache(cache_dir, sym, "H1")
                if bars is None:
                    missing.append(f"{sym}/H1")
                else:
                    raw[(sym, "H1")] = bars
        if missing:
            print(f"--offline: missing cache for {', '.join(missing)} "
                  f"(cache_dir={cache_dir}) — run without --offline once")
            return 2
        print(f"[tv-export] OFFLINE mode: bars from {cache_dir}")
    else:
        fetched = asyncio.run(fetch_tv(symbol_cfgs, tf_requests,
                                       float(cfg["fetch"].get("timeout_seconds", 120.0))))
        for (sym, tf), bars in fetched.items():
            raw[(sym, tf)] = bars
            _write_cache(cache_dir, sym, tf, bars)
        failed = [sc["symbol"] for sc in symbol_cfgs
                  if not any(k[0] == str(sc["symbol"]).upper() for k in raw)]
        if failed:
            print(f"[tv-export] WARNING: no bars for {', '.join(failed)} "
                  "— symbol skipped (fetch returned nothing)")

    # ── corpus export per symbol ───────────────────────────────────────────
    now = time.time()
    report = {"engine": cfg["engine"], "generated": datetime.now(timezone.utc)
              .strftime("%Y.%m.%d %H:%M"), "mode": "offline" if a.offline else "live",
              "config": {"path": cfg["_path"], "status": cfg["_status"]},
              "start": start_epoch, "out_dir": out_dir, "symbols": []}
    total_rows = 0
    for sc in symbol_cfgs:
        sym = str(sc["symbol"]).upper()
        m15 = closed_bars(_trim(raw.get((sym, "M15")) or [], start_epoch),
                          TF_SECONDS["M15"], now) if need_m15 else []
        h1 = closed_bars(_trim(raw.get((sym, "H1")) or [], start_epoch),
                         TF_SECONDS["H1"], now) if need_h1 else []
        m30 = aggregate_m30(m15) if tfs.get("M30", True) else []
        bars_by_tf = {"M15": m15 if tfs.get("M15", True) else [],
                      "M30": m30, "H1": h1}
        if not any(bars_by_tf.values()):
            print(f"[tv-export] {sym}: no closed bars — skipped")
            continue
        out_path = os.path.join(out_dir, f"CRT_CONT_RESEARCH_{sym}.csv")
        stats = tv_corpus.export_symbol(
            sym, str(sc.get("tradingview_symbol", "")), bars_by_tf, grid,
            windows, core, float(sc.get("spread_price", 0.0)),
            float(sc.get("slip_price", 0.0)), float(sc.get("point", 1.0)),
            int(sc.get("digits", 5)), out_path)
        stats.pop("path", None)
        # span months per TF (simple, from the bars themselves)
        for s in stats["tfs"]:
            bars = bars_by_tf.get(s["tf"]) or []
            if bars:
                s["span_months"] = round(_span_months(bars[0]["t"], bars[-1]["t"]), 1)
                if s["span_months"] < 30.0:
                    s["thin_for_walkforward"] = True
        report["symbols"].append(stats)
        total_rows += stats["rows"]
        tf_bits = ", ".join(
            f"{s['tf']}: {s['bars']:,} bars {s['t_min'][:10]}->{s['t_max'][:10]}"
            f" ({s.get('span_months', '?')}m) setups {s['setups']:,}"
            + (" THIN<30m" if s.get("thin_for_walkforward") else "")
            for s in stats["tfs"])
        print(f"[tv-export] {sym}: {stats['rows']:,} variant rows "
              f"({stats['setups']:,} setups, {stats['gap_skipped']} gap-skipped) "
              f"-> {out_path}")
        print(f"            {tf_bits}")

    if not report["symbols"]:
        print("[tv-export] nothing exported (no bars for any symbol)")
        return 2
    report["total_rows"] = total_rows
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "tv_export_report.json"), "w",
              encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    # ── one #crt signal (fail-open via the notifier itself) ────────────────
    discord_cfg = load_discord_config(a.discord_config)
    notifier = discord_notify.from_config(discord_cfg)
    if a.no_discord or not (cfg.get("discord") or {}).get("enabled", True):
        notifier.enabled = False
    else:
        notifier.label = str((cfg.get("discord") or {}).get("label", "CRT-TV"))
    try:
        lines = [f"mode {report['mode']} | config {cfg['_status']} | "
                 f"{total_rows:,} variant rows -> {os.path.basename(out_dir)}/"]
        for st in report["symbols"]:
            for s in st["tfs"]:
                lines.append(
                    f"{st['symbol']:<7}{s['tf']:<4}{s['bars']:>7,} bars "
                    f"{s['t_min'][:10]} -> {s['t_max'][:10]} "
                    f"({s.get('span_months', '?')}m) setups {s['setups']:>6,}"
                    + ("  THIN" if s.get("thin_for_walkforward") else ""))
        lines.append("basis=tdva_tv (the engine's own ports on the engine's feed) "
                     "| next: python scripts/run_crt_lab.py --data-dir "
                     + os.path.relpath(out_dir, REPO_ROOT))
        notifier.notify("tvexport", f"corpus export {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
                        "\n".join(lines))
    finally:
        notifier.close()

    print(f"\n[tv-export] done: {total_rows:,} rows in {out_dir}"
          f" | report: tv_export_report.json")
    print(f"[tv-export] next: python scripts/run_crt_lab.py --data-dir "
          f"{os.path.relpath(out_dir, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
