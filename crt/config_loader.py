"""config_loader.py — the CRT-0 walk-forward artifact -> engine options (CRT-1).

Port of the EA's ``LoadOptConfig``/``FindCfg`` reading ``CRT_CONT_<SYM>.csv``
— except the Kairos engine reads the JSON artifact minted by CRT-0
(``config/crt_cont.json`` via ``regime_edge_crt/report.py::build_artifact``,
commit b49e2bc). Row keys (exact, from the mint):

    symbol, tf, dir, session, model, entry_buf, sl_buf, r, hold, max_sweep,
    chop, vol_mask, c3_dir, enabled, base_ev, base_sd, oos_n, oos_t,
    train_end, generated

EA semantics preserved verbatim:

- ``session`` is the INT session id (0 OUT / 1 NY / 2 ASIA) — the mint writes
  ``session_id``;
- ``model``: "C3C" -> 1, "CONT" -> 2, anything else -> 0 (REV STOP@C2);
- ``vol_mask <= 0`` -> 7 (all vol regimes);
- rows with ``r <= 0`` or ``hold < 1`` are SKIPPED (the EA's ``continue``);
- duplicate (tf, dir, session) rows: FIRST wins (the EA's linear FindCfg
  scan returns the first match); the optimizer emits one row per key anyway;
- unknown ``tf`` values are skipped (Kairos monitors M15/M30/H1 only);
- fail-open: a missing/unparsable artifact yields zero options — every
  engine idles (the engine never runs without real-data evidence).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

TF_SECONDS = {"M15": 900, "M30": 1800, "H1": 3600}
MODEL_MAP = {"C3C": 1, "CONT": 2}
SESS_IDS = {"OUT": 0, "NY": 1, "ASIA": 2}


@dataclass
class OptionCfg:
    """One walk-forward option row (EA ``OptCfg``)."""

    symbol: str
    tf: str
    dir: str                      # setup direction "BUY"/"SELL" (CONT trades opposite)
    sess: int                     # 0 OUT / 1 NY / 2 ASIA
    model: int                    # 0 REV STOP@C2 / 1 C3C / 2 CONT
    eb: float                     # entry buffer x ATR
    sb: float                     # SL buffer x ATR
    r: float                      # fixed R target
    hold: int                     # bars after C3 (time exit)
    max_sweep: float              # 0 disables the sweep filter
    chop: int                     # 1 = chop filter on
    vol_mask: int                 # bitmask 1 low / 2 mid / 4 high; 7 = all
    c3_dir: int                   # 1 = C3C requires directional close
    enabled: bool
    base_ev: float
    base_sd: float
    oos_n: int
    oos_t: float
    train_end: datetime | None = None
    generated: Any = None
    # runtime monitor state (ComputeMonitors fields)
    state: int = 0                # EM_NORMAL 0 / EM_CAUTION 1 / EM_OFF 2
    s_cusum: float = 0.0
    live_n: int = 0
    live_ev: float = 0.0
    roll_ev: float = 0.0
    # v5 rolling window (ComputeRolling fields)
    rl_n: int = 0
    rl_ev: float = 0.0

    @property
    def key(self) -> tuple[str, int]:
        return (self.dir, self.sess)

    @property
    def tf_seconds(self) -> int:
        return TF_SECONDS[self.tf]

    def state_name(self) -> str:
        if not self.enabled:
            return "DISABLED"
        return {0: "NORMAL", 1: "CAUTION", 2: "OFF"}.get(self.state, "NORMAL")


def _parse_train_end(raw: Any) -> datetime | None:
    """Parse the mint's train_end ("YYYY.MM.DD HH:MM", "YYYY.MM.DD", or ISO).

    Naive values are pinned to UTC (the research corpus dates are exchange
    dates; the engine compares against UTC epoch boundaries).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%Y.%m.%d %H:%M", "%Y.%m.%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _to_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _to_int(raw: Any, default: int = 0) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def parse_rows(rows: list[dict[str, Any]], symbol_filter: str | None = None) -> list[OptionCfg]:
    """Artifact rows -> OptionCfg list (EA LoadOptConfig row rules verbatim)."""
    out: list[OptionCfg] = []
    seen: set[tuple[str, str, str, int]] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        tf = str(row.get("tf") or "").strip().upper()
        if tf not in TF_SECONDS:
            continue
        if symbol_filter is not None and symbol != symbol_filter.upper():
            continue
        d = str(row.get("dir") or "").strip().upper()
        if d not in ("BUY", "SELL"):
            continue
        sess_raw = row.get("session")
        if isinstance(sess_raw, str):
            sess = SESS_IDS.get(sess_raw.strip().upper(), _to_int(sess_raw, -1))
        else:
            sess = _to_int(sess_raw, -1)
        if sess not in (0, 1, 2):
            continue
        key = (symbol, tf, d, sess)
        if key in seen:
            continue    # FindCfg first-match wins; duplicates are inert
        seen.add(key)
        model = MODEL_MAP.get(str(row.get("model") or "").strip().upper(), 0)
        r = _to_float(row.get("r"))
        hold = _to_int(row.get("hold"))
        if r <= 0 or hold < 1:
            continue    # EA: invalid rows are never counted
        vol_mask = _to_int(row.get("vol_mask"), 7)
        if vol_mask <= 0:
            vol_mask = 7
        out.append(OptionCfg(
            symbol=symbol, tf=tf, dir=d, sess=sess, model=model,
            eb=_to_float(row.get("entry_buf")),
            sb=_to_float(row.get("sl_buf")),
            r=r, hold=hold,
            max_sweep=_to_float(row.get("max_sweep")),
            chop=_to_int(row.get("chop")),
            vol_mask=vol_mask,
            c3_dir=_to_int(row.get("c3_dir")),
            enabled=bool(_to_int(row.get("enabled"))),
            base_ev=_to_float(row.get("base_ev")),
            base_sd=_to_float(row.get("base_sd")),
            oos_n=_to_int(row.get("oos_n")),
            oos_t=_to_float(row.get("oos_t")),
            train_end=_parse_train_end(row.get("train_end")),
            generated=row.get("generated"),
        ))
    return out


def load_artifact(path: str) -> tuple[dict[str, Any], list[OptionCfg]]:
    """Read the minted artifact. Fail-open: (meta, []) on any error.

    The mtime is cheap to poll — ``jobs.py`` hot-reloads engines when the
    operator re-mints (the feature_flags mtime pattern).
    """
    if not path or not os.path.exists(path):
        return {}, []
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}, []
    if not isinstance(doc, dict):
        return {}, []
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    rows = doc.get("rows") if isinstance(doc.get("rows"), list) else []
    return meta, parse_rows(rows)


def options_by_symbol_tf(options: list[OptionCfg]) -> dict[tuple[str, str], dict[tuple[str, int], OptionCfg]]:
    """{(symbol, tf) -> {(dir, sess) -> OptionCfg}} for the engine registry."""
    out: dict[tuple[str, str], dict[tuple[str, int], OptionCfg]] = {}
    for opt in options:
        out.setdefault((opt.symbol, opt.tf), {})[opt.key] = opt
    return out
