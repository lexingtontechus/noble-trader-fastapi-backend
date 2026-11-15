"""config.py — the SELF-CONTAINED CRT config (operator requirement, 2026-11-15).

Every CRT knob lives in ``config/crt/*.yaml``; the platform's config files get
exactly ONE additive key (``crt_mode``, the CRT-1 ladder, registered in
``config/feature_flags.yaml`` but never read by this research twin). Nothing
scatters. The loader fails OPEN: a missing/unparsable yaml falls back to the
defaults below — and says so in ``status`` so artifacts never silently claim
a config that was not applied.

Defaults are the MT4 toolkit's argparse defaults EXACTLY (the Task 67
assessment's fidelity checklist §7):

  walk-forward   z=1.28, min_train_n=30, min_oos_n=30, min_oos_ev=0.02,
                 min_oos_t=1.0, require_final_lcb=True, folds 24m/6m rolling,
                 vol bands 0.33/0.67, baseline='lcb'
  decay monitor  kappa_frac=0.5, kappa_min=0.05, h_caution=12, h_off=18,
                 roll_n=60, roll_off_ev=-0.10, react_n=10, caution_mult=0.5
  baseline (v3)  max_sweep=0.75, chop=1, sessions={NY,ASIA}, model CONT,
                 eb=0, sb=0, r=1.5, hold=24

Changing a default here without changing the MT4 toolkit it mirrors is a
fidelity break — change the yaml, not this file.
"""
from __future__ import annotations

import copy
import os
from typing import Any

try:  # stdlib-fallback: the twin runs wherever pandas/numpy exist
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a repo requirement
    yaml = None

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAB_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "crt", "crt_lab.yaml")
DISCORD_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "crt", "crt_discord.yaml")

LAB_DEFAULTS: dict[str, Any] = {
    "engine": "regime_edge_crt v1",
    "data": {
        "raw_dir": os.path.join("data", "crt", "raw"),
        "file_glob": "CRT_CONT_RESEARCH_*.csv",
        "start": "",
        "models": "CONT",
    },
    "baseline": {
        "model": "CONT",
        "max_sweep": 0.75,
        "min_sweep": 0.0,
        "chop": 1,
        "sessions": "1,2",
        "eb": 0.0,
        "sb": 0.0,
        "r": 1.5,
        "hold": 24,
    },
    "walkforward": {
        "stage": 1,
        "train_months": 24,
        "test_months": 6,
        "anchored": False,
        "z": 1.28,
        "min_train_n": 30,
        "min_oos_n": 30,
        "min_oos_ev": 0.02,
        "min_oos_t": 1.0,
        "require_final_lcb": True,
        "vol_low": 0.33,
        "vol_high": 0.67,
        "baseline": "lcb",
        "prefix": "CRT_CONT_",
    },
    "decay": {
        "kappa_frac": 0.5,
        "kappa_min": 0.05,
        "h_caution": 12.0,
        "h_off": 18.0,
        "roll_n": 60,
        "roll_off_ev": -0.10,
        "react_n": 10,
        "caution_mult": 0.5,
    },
    "report": {
        "out_root": os.path.join("worklog", "kairos", "reports", "regime_edge_crt"),
    },
    "artifact": {
        "path": os.path.join("config", "crt_cont.json"),
    },
    "selftest": {
        "seed": 7,
        "months": 41,
        "symbols": ["XAUUSD", "EURUSD"],
    },
}

DISCORD_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "channel_label": "#crt",
    "username": "CRT-0 Lab",
    "label": "CRT-0",
    "webhook_url": "",
    "timeout_seconds": 10.0,
    "max_attempts": 2,
    "rate_limit": {
        "messages_per_window": 5,
        "window_seconds": 2.0,
        "max_sleep": 3.0,
    },
    "max_content_chars": 1900,
    "events": {
        "handshake": True,
        "baseline": True,
        "walkforward": True,
        "mint": True,
        "error": True,
    },
}


class CRTLabError(RuntimeError):
    """Fatal lab error (the port's sys.exit paths; the CLI maps this to rc=2)."""


def _read_yaml(path: str) -> tuple[dict, str]:
    """Return (mapping, status). Fail-open: never raises."""
    if not path or not os.path.exists(path):
        return {}, "missing"
    if yaml is None:
        return {}, "no-pyyaml"
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return {}, f"unparsable: {exc}"
    if doc is None:
        return {}, "empty"
    if not isinstance(doc, dict):
        return {}, "not-a-mapping"
    return doc, "loaded"


def _merge(base: dict, over: dict) -> dict:
    """Shallow-per-section merge: yaml sections override defaults whole-key."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(copy.deepcopy(value))
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_lab_config(path: str | None = None) -> dict:
    """Load config/crt/crt_lab.yaml over LAB_DEFAULTS. Adds ``_status``/``_path``."""
    cfg_path = path or LAB_CONFIG_PATH
    doc, status = _read_yaml(cfg_path)
    cfg = _merge(LAB_DEFAULTS, doc)
    cfg["_path"] = cfg_path
    cfg["_status"] = status
    return cfg


def load_discord_config(path: str | None = None) -> dict:
    """Load config/crt/crt_discord.yaml over DISCORD_DEFAULTS. Fail-open.

    The webhook URL resolves ENV-FIRST (operator instruction, 2026-11-15):
    ``DISCORD_WEBHOOK_CRT`` overrides the yaml ``webhook_url`` so the secret
    lives in the environment, not the repo. The yaml value is a documented
    fallback for deployments that prefer file config. Neither set → the
    sender stays disabled (fail-open: signals skipped, ``_status`` says so).
    """
    cfg_path = path or DISCORD_CONFIG_PATH
    doc, status = _read_yaml(cfg_path)
    cfg = _merge(DISCORD_DEFAULTS, doc)
    env_url = (os.environ.get("DISCORD_WEBHOOK_CRT") or "").strip()
    if env_url:
        cfg["webhook_url"] = env_url
        cfg["_webhook_source"] = "env:DISCORD_WEBHOOK_CRT"
    else:
        cfg["_webhook_source"] = "yaml" if cfg.get("webhook_url") else "unset"
    cfg["_path"] = cfg_path
    cfg["_status"] = status
    if not cfg.get("webhook_url"):
        cfg["enabled"] = False
    return cfg


def resolve_repo_relative(path_value: str, cfg_path: str) -> str:
    """Resolve a repo-relative path from a config value.

    Absolute paths pass through; relative values resolve against REPO_ROOT
    (NOT the cwd) so the lab behaves identically from any launch directory.
    """
    if os.path.isabs(path_value):
        return path_value
    return os.path.normpath(os.path.join(REPO_ROOT, path_value))
