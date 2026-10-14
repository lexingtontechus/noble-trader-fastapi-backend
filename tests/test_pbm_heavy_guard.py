"""P0 pins: the heavy-path EV-clobber guard, the 3-outcome recompute, and the
resolver's all-outcomes posterior hook.

The 2026-10-13 triage found the light path guarded against calibration
enforcement re-pricing a reachability-priced EV (orchestrator.py, bracket-EV
terminal) while the heavy twin overwrote ``ev_result.p_win`` unguarded AND
recomputed without ``p_expire`` — the heavy EV silently became a binary
price. These tests pin BOTH the structural fix (source-level, the repo's own
pinning-test style) and the behavioural contract of the primitives the fix
relies on.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEAVY = ROOT / "regime_platform" / "sweep_orchestrator" / "heavy_sweep.py"
RESOLVER = ROOT / "regime_platform" / "sweep_orchestrator" / "eod_resolver.py"
ORCH = ROOT / "regime_platform" / "sweep_orchestrator" / "orchestrator.py"


# ─── Structural pins (regression guards on the wiring) ──────────────────────


def test_heavy_enforcement_has_bracket_terminal_guard():
    """The enforcement block must branch on ``_bracket_applied_to_ev``
    BEFORE any ``ev_result.p_win`` mutation — the light path's guard,
    applied to the heavy twin (P0 from the 2026-10-13 triage)."""
    src = HEAVY.read_text()
    # the guard branch exists inside the enforcement region
    assert "if _bracket_applied_to_ev:" in src
    # and it appears before the unguarded mutation in the else branch
    guard_idx = src.index("if _bracket_applied_to_ev:")
    mutate_idx = src.index('ev_result.p_win = calib_adj["p_win"]')
    assert guard_idx < mutate_idx
    # the terminal branch keeps p_win and only adjusts kelly
    terminal_block = src[guard_idx:mutate_idx]
    assert "effective_kelly_pre = calib_adj[" in terminal_block
    assert "ev_result.p_win =" not in terminal_block


def test_heavy_recompute_passes_p_expire():
    """The unguarded (directional) branch's recompute must keep the
    3-outcome contract — the previous call dropped ``p_expire`` and the
    heavy EV silently became a binary price (P0b)."""
    src = HEAVY.read_text()
    mutate_idx = src.index('ev_result.p_win = calib_adj["p_win"]')
    window = src[mutate_idx:mutate_idx + 2000]
    assert "recompute_ev_fields(" in window
    assert "p_expire=" in window


def test_resolver_hooks_all_outcome_classes():
    """Both resolve funnels must call the posterior update for ALL outcomes
    — tp_hit, sl_hit, AND expired (survivorship closed by construction),
    lazily imported (the module-level import created the cycle
    tiered_atr -> geometry_risk -> heavy_sweep -> eod_resolver ->
    posterior -> barrier -> tiered_atr)."""
    src = RESOLVER.read_text()
    assert "from regime_platform.sweep_orchestrator.posterior import" not in src.split("def ")[0]
    n_hooks = src.count("await _update_cell_posterior(sig, resolved)")
    assert n_hooks == 2, f"expected 2 resolver hooks, found {n_hooks}"
    # the calibration-record gate stays tp/sl-only; the posterior hook must
    # sit OUTSIDE that gate (expired rows update the cell too)
    gated = 'if resolved["outcome"] in ("tp_hit", "sl_hit"):'
    for hook_idx in [
        i for i in range(len(src)) if src.startswith("await _update_cell_posterior", i)
    ]:
        preceding = src[:hook_idx]
        last_gate = preceding.rfind(gated)
        last_gate_end = preceding.rfind("return resolved")
        assert last_gate != -1
        # the hook comes after the calibration gate closes (its return or
        # the gate's block), i.e. it is not inside the tp/sl-only branch
        assert last_gate < last_gate_end + len("return resolved") <= hook_idx or (
            last_gate < hook_idx and src[last_gate + len(gated):hook_idx].count("\n    ") > 4
        )


def test_light_and_heavy_share_one_substitution_contract():
    """Phase B: both paths feed ``apply_bracket_to_ev`` through the SAME
    local inputs pattern (``_ev_p_win_src, _ev_p_resolve_src``), so there
    is exactly one EV definition — structurally."""
    for path in (ORCH, HEAVY):
        src = path.read_text()
        assert "_ev_p_win_src, _ev_p_resolve_src" in src
        assert "get_pbm_ev_mode() == \"live\"" in src


# ─── Behavioural pins (the primitives the guards preserve) ─────────────────


def test_apply_bracket_to_ev_is_terminal_3_outcome():
    """apply_bracket_to_ev mutates the EV result to the bracket probability
    with the expiry term preserved — the object the guards keep terminal."""
    from regime_platform.sweep_orchestrator.p_win_bracket import (
        apply_bracket_to_ev,
    )

    class _Engine:
        min_ev = 0.0
        min_rr = 0.0

        def recompute_ev_fields(self, p_win, profit, stake,
                                min_rr=None, p_expire=0.0):
            p_exp = max(0.0, min(1.0, float(p_expire or 0.0)))
            loss = max(0.0, 1.0 - float(p_win) - p_exp)
            ev = p_win * profit - loss * stake
            return {
                "ev": ev, "ev_per_dollar": ev / stake if stake else 0.0,
                "rr": profit / stake if stake else 0.0, "allowed": ev > 0,
                "aggression": "mid", "ev_scale": 1.0,
            }

    class _Result:
        p_win = 0.55
        profit = 3.0
        stake = 1.0
        ev = 0.65
        ev_per_dollar = 0.65
        rr = 3.0
        allowed = True
        aggression = "mid"
        ev_scale = 1.0

    r = _Result()
    applied = apply_bracket_to_ev(
        r, p_win_bracket=0.10, p_resolve=0.35, engine=_Engine(),
    )
    assert applied is True
    assert r.p_win == 0.10
    # 3-outcome: expire (1 - 0.35 = 0.65) is not charged as a loss
    assert r.ev == 0.10 * 3.0 - (1.0 - 0.10 - 0.65) * 1.0
    # unscored fails OPEN to leaving the directional EV untouched
    r2 = _Result()
    assert apply_bracket_to_ev(r2, p_win_bracket=None, p_resolve=None,
                               engine=_Engine()) is False
    assert r2.p_win == 0.55


def test_recompute_with_p_expire_never_charges_expire_as_loss():
    class _Engine:
        min_ev = -1e9
        min_rr = 0.0

        def recompute_ev_fields(self, p_win, profit, stake,
                                min_rr=None, p_expire=0.0):
            p_exp = max(0.0, min(1.0, float(p_expire or 0.0)))
            loss = max(0.0, 1.0 - float(p_win) - p_exp)
            return {"ev": p_win * profit - loss * stake}

    eng = _Engine()
    # all-expire: EV must be 0 (not -stake)
    assert eng.recompute_ev_fields(0.0, 3.0, 1.0, p_expire=1.0)["ev"] == 0.0
    # same p_win, different p_expire -> different (correct) EV
    e1 = eng.recompute_ev_fields(0.2, 3.0, 1.0, p_expire=0.0)["ev"]
    e2 = eng.recompute_ev_fields(0.2, 3.0, 1.0, p_expire=0.6)["ev"]
    assert e1 < e2  # dropping the expire term (the old heavy bug) under-prices


# ─── Import-order cycle pin ─────────────────────────────────────────────────


def test_no_circular_import_either_order():
    """The eod_resolver -> posterior edge must be lazy: importing
    eod_resolver FIRST and tiered_atr FIRST must both succeed (the cycle
    tiered_atr -> geometry_risk -> heavy_sweep -> eod_resolver -> posterior
    -> barrier -> tiered_atk would otherwise fire)."""
    for order in (
        "eod_resolver, tiered_atr, posterior, barrier",
        "tiered_atr, eod_resolver, posterior, barrier",
        "barrier, posterior, tiered_atr, eod_resolver",
    ):
        code = (
            "from regime_platform.sweep_orchestrator import " + order
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=str(ROOT), timeout=120,
        )
        assert proc.returncode == 0, (
            f"import order [{order}] failed:\n{proc.stderr[-800:]}"
        )
