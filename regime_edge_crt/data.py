"""data.py — compact-path CSV ingest + provenance manifest.

Ports the loading section of ``crt_ev_optimizer.py`` VERBATIM (read_meta,
load) plus the Task-67-sanctioned additions: sha256 input manifest and a
required-schema check. The CSV schema is ``CRT_EV_Research_Export.mq4`` v1.10:

    symbol,tf,dir,c2_time,c3_time,session,sweep_atr,chop_wick,chop_overlap,
    dir_changes,chop_pass,atr_pts,atr_pct,c1_swing,swept_pd,c3_body,c3_dir_ok,
    model,entry_buf,sl_buf,filled,risk_pts,entry_cost_r,exit_slip_r,bars_avail,
    bars_sl,b_<R>...,c_<H>...

with a ``#meta,point=...,digits=...,spread_pts=...,slip_pts=...,clock=...,
gmt_off=...,nyclose=...`` line within the first 3 lines (MQL4 FILE_ANSI →
latin-1 decode; CRLF terminators). ``dir`` is always the SETUP direction; a
CONT row on a BUY setup is a SELL trade.
"""
from __future__ import annotations

import hashlib
import os
import sys

import pandas as pd

from .config import CRTLabError

REQUIRED_COLUMNS = [
    "symbol", "tf", "dir", "c2_time", "c3_time", "session", "sweep_atr",
    "chop_pass", "atr_pct", "c3_dir_ok", "model", "entry_buf", "sl_buf",
    "filled", "risk_pts", "entry_cost_r", "exit_slip_r", "bars_avail", "bars_sl",
]


def read_meta(path: str) -> dict:
    """The #meta line (spread/point/clock cost-parity provenance). Verbatim port."""
    meta: dict = {}
    with open(path, "r", encoding="latin-1") as f:
        for _ in range(3):
            line = f.readline()
            if line.startswith("#meta"):
                for kv in line.strip().split(",")[1:]:
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        meta[k] = v
    return meta


def load(paths):
    """Load + concat the export CSVs. Verbatim port (warning on empty, sys.exit→CRTLabError)."""
    frames = []
    metas = {}
    for p in paths:
        df = pd.read_csv(p, comment="#", encoding="latin-1")
        df.columns = [c.strip() for c in df.columns]
        if df.empty:
            print(f"warning: {p} is empty", file=sys.stderr)
            continue
        validate_schema(df, p)
        metas[str(df["symbol"].iloc[0])] = read_meta(p)
        frames.append(df)
    if not frames:
        raise CRTLabError("no data")
    df = pd.concat(frames, ignore_index=True)
    df["c3_time"] = pd.to_datetime(df["c3_time"], format="%Y.%m.%d %H:%M")
    df["c2_time"] = pd.to_datetime(df["c2_time"], format="%Y.%m.%d %H:%M")
    return df, metas


def validate_schema(df: pd.DataFrame, path: str) -> None:
    """Additive guard: a malformed export fails loudly, not as silent NaN math."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise CRTLabError(
            f"{os.path.basename(path)}: missing CRT export columns {missing} — "
            "regenerate with CRT_EV_Research_Export.mq4 v1.10"
        )
    if not any(c.startswith("b_") for c in df.columns):
        raise CRTLabError(f"{os.path.basename(path)}: no b_<R> grid columns")
    if not any(c.startswith("c_") for c in df.columns):
        raise CRTLabError(f"{os.path.basename(path)}: no c_<H> grid columns")


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_manifest(paths) -> list:
    """Provenance stamps (Task 67 §2): every ingested file's identity travels
    into the run record and the minted artifact."""
    rows = []
    for p in paths:
        with open(p, "r", encoding="latin-1") as fh:
            lines = fh.readlines()
        data_rows = sum(1 for ln in lines[1:] if not ln.startswith("#"))
        rows.append({
            "path": os.path.basename(p),
            "sha256": sha256_of(p),
            "data_rows": data_rows,
        })
    return rows
