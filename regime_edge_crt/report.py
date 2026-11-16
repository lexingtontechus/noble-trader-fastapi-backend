"""report.py — the minted artifacts: config/crt_cont.json + the run record.

Mirrors the repo's artifact conventions (claims_vol_fan_recalib.json / the
edge-lab edge_lab.json): a ``meta`` block carrying FULL provenance (engine,
source zip, runid, thresholds echo, folds, sha256 input manifest, honesty
notes) + ``rows`` mirroring the per-symbol EA deploy CSV columns exactly
(what CRT-1's ``crt/config_loader.py`` will read). Every float is coerced
json-safe (NaN/inf → null) — a NaN in a JSON artifact is a lie waiting to
be parsed.
"""
from __future__ import annotations

import json
import math

import numpy as np

from . import ENGINE, SOURCE_ZIP

HONESTY_NOTES = [
    "Rolling window overlaps the training data when the config was trained up "
    "to today — a live health check, not a validation (README_v5_CONT.md).",
    "Thin H1 rolling window: ~5-20 setups per 45d option; the pooled fallback "
    "is a floor, not proof of an edge.",
    "Setups overlap across TFs and can overlap in time within a bucket; the "
    "EA's one-position-per-symbol rule will skip some — live totals run lower.",
    "Costs are the export's spread/slippage model (#meta line), same basis as "
    "the EA's labeler; no tick data.",
]


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return v if math.isfinite(v) else None
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    return str(obj)


def write_json(path: str, payload) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_jsonable(payload), fh, indent=2, ensure_ascii=False, allow_nan=False)
        fh.write("\n")
    return path


def _basis_block(metas: dict | None) -> dict:
    """Basis stamping (Path B, 2026-11-16): the corpus CSVs' ``#meta`` keys
    travel into the artifact when present — a TV-basis export stamps
    ``basis=tdva_tv`` and the artifact says so (auditable end to end).
    MT4 exports carry no ``basis`` key -> empty dict (byte-identical for
    them; the param is additive and optional). Returns the INNER mapping
    ``{symbol: basis}`` — callers assign it under ``meta.basis``.
    """
    if not metas:
        return {}
    return {str(sym): str(m.get("basis")) for sym, m in metas.items()
            if isinstance(m, dict) and m.get("basis")}


def build_artifact(cfg: dict, wf: dict, manifest: list, runid: str,
                   metas: dict | None = None) -> dict:
    """The CRT-1 engine artifact: meta (provenance + thresholds echo) + rows
    (the deploy config per picked option — schema mirrors the EA CSV).
    ``metas`` (optional): the loader's per-symbol ``#meta`` dicts — stamps
    the corpus basis block when the export carried one."""
    wf_cfg = cfg["walkforward"]
    dec_cfg = cfg["decay"]
    artifact = {
        "meta": {
            "engine": ENGINE,
            "source": SOURCE_ZIP,
            "runid": runid,
            "generated": wf.get("generated"),
            "train_end": wf.get("train_end"),
            "data_span": {"from": wf.get("t_min"), "to": wf.get("t_max"),
                          "rows": wf.get("n_rows")},
            "r_grid": wf.get("r_grid"),
            "holds": wf.get("holds"),
            "folds": {"n": len(wf.get("folds") or []),
                      "train_months": wf_cfg["train_months"],
                      "test_months": wf_cfg["test_months"],
                      "anchored": wf_cfg["anchored"],
                      "windows": wf.get("folds")},
            "thresholds": {
                "stage": wf_cfg["stage"], "z": wf_cfg["z"],
                "min_train_n": wf_cfg["min_train_n"], "min_oos_n": wf_cfg["min_oos_n"],
                "min_oos_ev": wf_cfg["min_oos_ev"], "min_oos_t": wf_cfg["min_oos_t"],
                "require_final_lcb": wf_cfg["require_final_lcb"],
                "vol_low": wf_cfg["vol_low"], "vol_high": wf_cfg["vol_high"],
                "baseline": wf_cfg["baseline"],
            },
            "decay": dict(dec_cfg),
            "inputs": manifest,
            "honesty_notes": list(HONESTY_NOTES),
            "config_status": cfg.get("_status"),
        },
        "rows": wf.get("deploy_rows") or [],
    }
    basis = _basis_block(metas)
    if basis:
        artifact["meta"]["basis"] = basis
    return artifact


def build_run_record(cfg: dict, runid: str, manifest: list, baseline: dict | None,
                     wf: dict | None, discord_events: list,
                     metas: dict | None = None) -> dict:
    """crt_lab.json — the full run record (config echo + A0 + walk-forward +
    the Discord signal trail). Research provenance, one file, per runid.
    ``metas`` (optional): per-symbol ``#meta`` dicts — stamps the basis."""
    buckets = []
    if wf:
        for b in wf.get("report_buckets") or []:
            if not b.get("deployed"):
                continue
            o = b["oos"]
            buckets.append({
                "symbol": b["symbol"], "tf": b["tf"], "dir": b["dir"],
                "session": b["session"], "model": b["model"],
                "enabled": bool(b["enabled"]), "reason": b["reason"],
                "n_setups": b["n_setups"], "n_configs": b["n_configs"],
                "oos": o, "gated_total": b["gated_total"],
                "gated_ev": b["gated_ev"], "folds_pos": b["folds_pos"],
                "stab": b["stab"], "off_share": b["off_share"],
            })
    record = {
        "meta": {
            "engine": ENGINE, "source": SOURCE_ZIP, "runid": runid,
            "config": {"path": cfg.get("_path"), "status": cfg.get("_status")},
            "discord_events_sent": discord_events,
        },
        "inputs": manifest,
        "baseline": baseline,
        "walkforward": {
            "deploy_rows": wf.get("deploy_rows") if wf else [],
            "buckets": buckets,
            "folds": wf.get("folds") if wf else [],
            "t_min": wf.get("t_min") if wf else None,
            "t_max": wf.get("t_max") if wf else None,
            "n_rows": wf.get("n_rows") if wf else None,
        },
    }
    basis = _basis_block(metas)
    if basis:
        record["meta"]["basis"] = basis
    return record


def build_summary_md(runid: str, cfg: dict, baseline: dict | None, wf: dict | None,
                     artifact_path: str | None) -> str:
    """summary.md — the headline verdicts a human reads first."""
    lines = [f"# CRT-0 lab run {runid}", "",
             f"engine: {ENGINE} | source: {SOURCE_ZIP} | config: {cfg.get('_status')} ({cfg.get('_path')})", ""]
    if baseline:
        lines += [
            "## Baseline (v3 filters, no optimization — no selection bias)",
            f"- setups {baseline['n_setups']:,} | filled {baseline['n_filled']:,} | "
            f"resolved {baseline['n_resolved']:,}",
            f"- data {baseline['data_min']} -> {baseline['data_max']}",
            "- EV per symbol x TF (R): see baseline_buckets.csv / the run console",
            "",
        ]
    if wf:
        dep = wf.get("deploy_rows") or []
        n_en = sum(1 for r in dep if r.get("enabled"))
        tot = sum((b["oos"]["total"] for b in wf["report_buckets"] if b.get("deployed")), 0.0)
        gat = sum((b["gated_total"] for b in wf["report_buckets"] if b.get("deployed")), 0.0)
        lines += [
            "## Walk-forward (Stage {})".format(cfg["walkforward"]["stage"]),
            f"- options deployed {len(dep)} | enabled {n_en}",
            f"- OOS total R (deployed): {tot:+.1f} | with decay gate: {gat:+.1f}",
            f"- folds {len(wf.get('folds') or [])} | data {wf['t_min']} -> {wf['t_max']}",
            "",
        ]
    if artifact_path:
        lines += ["## Artifact", f"- minted: `{artifact_path}` (CRT-1 reads this)", ""]
    lines += ["## Honesty notes"] + [f"- {n}" for n in HONESTY_NOTES]
    return "\n".join(lines) + "\n"
