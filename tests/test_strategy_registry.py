"""Strategy Registry R0 — loader tests.

Pins the contract R1's router depends on:
  * the catalog loads from config/strategy_registry.yaml,
  * fail-open to an EMPTY registry on a missing/corrupt file (degrades to
    "no routing", never a wrong routing),
  * hot-reload on mtime (edit takes effect without restart),
  * the phase-1 archetypes and the gold-buy mean_reversion predicate,
  * router mode ships at shadow, and get_router_mode() rejects bad values.

R0 ships nothing that consumes the registry; these tests lock the *shape* so
R1 can be built against a stable contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from regime_platform.core import feature_flags as ff


def test_registry_loads_and_has_phase1_archetypes():
    reg = ff.get_strategy_registry()
    assert isinstance(reg, dict) and reg, "registry must be non-empty"
    # Phase-1 set from the scope doc (momentum, mean_reversion live; the rest
    # declared-but-disabled).
    for name in ("momentum", "mean_reversion", "ma_trend", "fx_carry",
                 "range_fade", "breakout"):
        assert name in reg, f"missing archetype {name}"


def test_mean_reversion_predicate_is_the_gold_buy_pattern():
    mr = ff.get_strategy_archetype("mean_reversion")
    assert mr is not None
    assert mr["enabled"] is True
    assert mr["requires_opposes"] is True          # counter-trend (ID45 pattern)
    assert mr["requires_aligned"] is not True
    assert "metal" in mr["asset_classes"]          # gold, era-clean evidence
    assert mr["directions"] == ["buy"]             # the gold-BUY cohort
    # Trade shape: tight TP to the mean, overshoot-Z stop, short hold.
    assert mr["exit"]["target"] == "mean"
    assert mr["risk"]["overshoot_stop_z"] == 3.5
    assert mr["exit"]["time_stop_hours"] == 24


def test_momentum_is_aligned_and_has_no_reversion_stop():
    m = ff.get_strategy_archetype("momentum")
    assert m is not None and m["enabled"] is True
    assert m["requires_aligned"] is True
    assert m["exit"]["target"] == "far"
    assert m["risk"]["overshoot_stop_z"] is None   # no mean to invalidate


def test_disabled_archetypes_are_declared_but_off():
    for name in ("ma_trend", "fx_carry", "range_fade", "breakout"):
        blk = ff.get_strategy_archetype(name)
        assert blk is not None
        assert blk["enabled"] is False, f"{name} must ship disabled (no era-clean evidence)"


def test_loader_normalizes_predicate_defaults():
    """Every archetype block carries requires_opposes / requires_aligned /
    enabled after load, so R1's router reads them unconditionally — a block
    that omits one (e.g. mean_reversion omits requires_aligned) still yields a
    defined False rather than a KeyError or a silent 'no constraint'."""
    for name, blk in ff.get_strategy_registry().items():
        assert "requires_opposes" in blk, f"{name} missing requires_opposes"
        assert "requires_aligned" in blk, f"{name} missing requires_aligned"
        assert "enabled" in blk, f"{name} missing enabled"
        assert blk["requires_opposes"] in (True, False)
        assert blk["requires_aligned"] in (True, False)
        assert blk["enabled"] in (True, False)


def test_router_mode_ships_shadow_and_rejects_bad_values():
    assert ff.get_router_mode() == "shadow"
    policy = ff.get_router_policy()
    assert policy["default_on_no_match"] == "stand_by"
    # mean_reversion before momentum so a counter-trend gold buy is not
    # swallowed by the momentum rule.
    prec = policy["precedence"]
    assert prec.index("mean_reversion") < prec.index("momentum")


def test_missing_file_fails_open_to_empty_registry(monkeypatch):
    """A missing YAML degrades to an EMPTY catalog + a valid shadow policy —
    the router can then only route stand_by, never invent a strategy."""
    monkeypatch.setattr(ff, "_registry_cache", None)
    monkeypatch.setattr(ff, "_registry_mtime", None)
    monkeypatch.setattr(ff, "_REGISTRY_PATH", Path("/nonexistent/strategy_registry.yaml"))
    reg = ff.get_strategy_registry()
    assert reg == {}
    # Router policy still valid (safe defaults), mode still shadow.
    assert ff.get_router_mode() == "shadow"
    assert ff.get_router_policy()["default_on_no_match"] == "stand_by"


def test_hot_reload_on_mtime(monkeypatch, tmp_path):
    """Editing the file changes what the loader returns without a restart."""
    p = tmp_path / "strategy_registry.yaml"
    p.write_text(
        "strategy_registry:\n  momentum:\n    enabled: true\n"
        "router:\n  mode: live\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ff, "_registry_cache", None)
    monkeypatch.setattr(ff, "_registry_mtime", None)
    monkeypatch.setattr(ff, "_REGISTRY_PATH", p)

    assert ff.get_router_mode() == "live"          # parsed from the temp file
    assert "momentum" in ff.get_strategy_registry()

    # Rewrite with a bumped mtime -> next call re-parses.
    import os, time
    p.write_text(
        "strategy_registry:\n  momentum:\n    enabled: false\n"
        "router:\n  mode: shadow\n",
        encoding="utf-8",
    )
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert ff.get_router_mode() == "shadow"
    assert ff.get_strategy_archetype("momentum")["enabled"] is False
