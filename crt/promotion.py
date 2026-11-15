"""promotion.py — CRT-3: the leaderboard-gated live promotion.

The assessment's contract, verbatim (20261115_CRT_V5_CONT_INTEGRATION_
ASSESSMENT.md §6): "the claims leaderboard arbitrates promotion to ``live``
exactly as it arbitrates the TSFM sidecar — no operator eyeballing; this
closes the loop with the lab's standing question ('is the sidecar/CRT-class
edge real after costs?')".

The evidence source is the CRT-2 claims ledger — resolved ``crt_option``
claims, the same pooled kernels ``/crt/claims`` serves (single source:
``crt.claims.aggregate_evidence``). The bars are PRE-REGISTERED in
``config/crt/crt_engine.yaml`` (the self-contained config contract) and are
the optimizer's OWN enable thresholds applied to live paper evidence —
the same numbers that minted the config gate its live promotion:

- n >= 30            (optimizer: OOS n >= 30)
- EV > 0.02 R        (optimizer: OOS EV > 0.02 — after costs by construction:
                      the labels carry the spread/slippage cost model)
- t >= 1.0           (optimizer: t >= 1.0)
- LCB > 0 at z=1.28   (optimizer: final LCB > 0 — the max-LCB walk-forward
                      convention)
- no ENABLED option in decay OFF (engine health — the CUSUM kill-switch has
  not fired; required by default)

Enforcement lives in ``crt/jobs.py``: ``crt_mode: live`` with unmet bars
runs PAPER semantics and records the refusal (never silent); bars met set
the live-dispatch flag (the #crt signal stream becomes the actionable
surface — the only live path that exists under the standing contract: MT5
untouched, no nt_signal_sim writes, no order execution). The verdict is
recomputed on a TTL cache (default 300s) — demotion on evidence decay is
automatic by construction, exactly like promotion.

Fail-open everywhere: a fetch failure yields the last cached verdict or an
honest empty-evidence verdict (n=0 → not eligible), never an exception.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("crt.promotion")

# pre-registered defaults (crt_engine.yaml promotion block overrides)
DEFAULT_BARS: dict[str, Any] = {
    "min_n": 30,
    "min_ev": 0.02,
    "min_t": 1.0,
    "lcb_z": 1.28,
    "require_decay_not_off": True,
    "cache_seconds": 300,
}

_CACHE: dict[str, Any] = {"ts": 0.0, "verdict": None}


def load_bars(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """The promotion bars — yaml promotion block over the pre-registered
    defaults (fail-open to defaults; the yaml is the self-contained
    authority)."""
    if cfg is None:
        try:
            from crt import jobs
            cfg = jobs._load_engine_config()   # noqa: SLF001 - same package
        except Exception:  # noqa: BLE001 - fail-open
            cfg = {}
    block = cfg.get("promotion") if isinstance(cfg, dict) else None
    bars = dict(DEFAULT_BARS)
    if isinstance(block, dict):
        for key in bars:
            if key in block and block[key] is not None:
                bars[key] = block[key]
    return bars


def decay_off_count(engines: list[Any] | None) -> int:
    """ENABLED options currently in decay OFF (state_name 'OFF' excludes
    DISABLED options by construction)."""
    n = 0
    for engine in engines or []:
        try:
            for opt in engine.options.values():
                if opt.enabled and opt.state_name() == "OFF":
                    n += 1
        except Exception:  # noqa: BLE001 - count what we can
            continue
    return n


def promotion_verdict(evidence: dict[str, Any], decay_off: int,
                      bars: dict[str, Any]) -> dict[str, Any]:
    """Pure verdict: eligible iff every pre-registered bar holds.

    ``evidence`` is ``crt.claims.aggregate_evidence`` output (n / ev_r /
    t_stat / lcb_r / win_rate_closed / ...). Reasons are human-readable,
    one per unmet bar — the refusal is never silent.
    """
    reasons: list[str] = []
    n = int(evidence.get("n") or 0)
    ev = float(evidence.get("ev_r") or 0.0)
    t_stat = float(evidence.get("t_stat") or 0.0)
    lcb = float(evidence.get("lcb_r") or 0.0)
    if n < int(bars["min_n"]):
        reasons.append(f"n {n} < {bars['min_n']} (resolved claims)")
    if ev <= float(bars["min_ev"]):
        reasons.append(f"EV {ev:+.3f}R not > {bars['min_ev']} (after costs)")
    if t_stat < float(bars["min_t"]):
        reasons.append(f"t {t_stat:.2f} < {bars['min_t']}")
    if lcb <= 0.0:
        reasons.append(f"LCB {lcb:+.3f}R not > 0 at z={bars['lcb_z']}")
    if bars.get("require_decay_not_off") and int(decay_off) > 0:
        reasons.append(f"{decay_off} enabled option(s) in decay OFF")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "evidence": evidence,
        "decay_off": int(decay_off),
        "bars": {k: bars[k] for k in DEFAULT_BARS},
    }


async def current_verdict(engines: list[Any] | None = None, *,
                          force: bool = False,
                          now: float | None = None) -> dict[str, Any]:
    """The TTL-cached verdict (the /crt/promotion + /crt/state source).

    Fetches resolved crt_option claims, aggregates through the SAME kernel
    as /crt/claims, counts decay-OFF options off the live engines, applies
    the pre-registered bars. Fail-open: a fetch error keeps the last cached
    verdict; no cache at all yields the honest empty-evidence verdict
    (n=0 → not eligible). Never raises.
    """
    now = time.time() if now is None else float(now)
    bars = load_bars()
    ttl = float(bars.get("cache_seconds", 300))
    cached = _CACHE.get("verdict")
    if cached is not None and not force and (now - float(_CACHE["ts"])) < ttl:
        return cached
    try:
        from crt import claims as crt_claims
        rows = await crt_claims.fetch_resolved_claims(limit=5000)
        evidence = crt_claims.aggregate_evidence(rows, lcb_z=float(bars["lcb_z"]))
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning("crt.promotion: evidence fetch failed: %s", exc)
        if cached is not None:
            return cached
        evidence = empty_evidence()
    verdict = promotion_verdict(evidence, decay_off_count(engines), bars)
    _CACHE.update({"ts": now, "verdict": verdict})
    return verdict


def empty_evidence() -> dict[str, Any]:
    """The honest zero-evidence aggregate (n=0 → every bar unmet)."""
    return {
        "n": 0, "n_tp": 0, "n_sl": 0, "n_expired": 0, "n_r": 0,
        "win_rate_closed": None, "win_rate_wilson95": None,
        "ev_r": 0.0, "sd_r": 0.0, "t_stat": 0.0, "lcb_r": 0.0,
        "lcb_z": DEFAULT_BARS["lcb_z"],
    }


def invalidate_cache() -> None:
    """Drop the TTL cache (tests / forced refresh)."""
    _CACHE.update({"ts": 0.0, "verdict": None})
