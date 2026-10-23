"""RM-1 retirement (2026-10-01): markov_source_mode -> legacy_hmm and
p_markov_hold_n_mode -> off.

Guards the decision recorded in
``worklog/kairos/reports/20261001_rm1_markov_source_eval.md``:
the trend_hmm shadow comparison was retired and p_markov_hold_n was
withdrawn from the EV blend because neither candidate source was
informative (legacy 0.527 weighted accuracy vs a 3-class coin flip;
65.9% state disagreement vs a 66.7% random baseline).

These are shipped-value assertions (same pattern as
``test_calibration_resolve_hourly.py::test_yaml_ships_live``) so a
silent revert is caught, plus the getter's fail-closed parsing and the
engine-level consequence: with no Markov source supplied, compute_ev()
renormalises it out.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from regime_platform.core import feature_flags as ff
from regime_platform.services.statistics.ev_engine import EVEngine, P_WIN_WEIGHTS_FULL

_YAML = Path(__file__).resolve().parents[1] / "config" / "feature_flags.yaml"


def _cfg() -> dict:
    return yaml.safe_load(_YAML.read_text(encoding="utf-8"))


# ─── 1. Shipped values ───────────────────────────────────────────────────


class TestShippedValues:
    def test_yaml_ships_legacy_hmm(self):
        """trend_hmm_shadow retired 2026-10-01 — the comparison answered
        negatively (legacy won 13/14 symbols; 65.9% disagreement vs 66.7%
        random). A revert here would silently resume the shadow compare."""
        assert _cfg()["markov_source_mode"] == "legacy_hmm"

    def test_yaml_ships_p_markov_hold_n_off(self):
        """p_markov_hold_n withdrawn from the EV blend 2026-10-01."""
        assert _cfg()["p_markov_hold_n_mode"] == "off"

    def test_defaults_match_shipped_yaml(self):
        """The module defaults must not drift from the shipped YAML — the
        defaults are the fallback when the YAML is unreadable."""
        assert ff._DEFAULTS["markov_source_mode"] == "legacy_hmm"
        assert ff._DEFAULTS["p_markov_hold_n_mode"] == "off"


# ─── 2. Getter parsing ───────────────────────────────────────────────────


class TestPMarkovHoldNModeGetter:
    @pytest.mark.parametrize("raw,expected", [
        (None, "off"),          # unset -> default
        ("off", "off"),
        ("live", "live"),
        ("LIVE", "live"),       # case-insensitive
        ("bogus", "off"),       # typo must NOT re-enable a withdrawn source
        ("", "off"),
    ])
    def test_parsing(self, raw, expected):
        cfg = {} if raw is None else {"p_markov_hold_n_mode": raw}
        with patch.object(ff, "_load", return_value=cfg):
            assert ff.get_p_markov_hold_n_mode() == expected

    def test_only_explicit_live_enables(self):
        """Fail-closed: anything that is not exactly 'live' is 'off'."""
        for raw in ("on", "true", "1", "yes", "enabled", "LIVE ", "liv"):
            with patch.object(ff, "_load", return_value={"p_markov_hold_n_mode": raw}):
                assert ff.get_p_markov_hold_n_mode() == "off", raw


# ─── 3. Engine-level consequence ─────────────────────────────────────────


class TestBlendDropsSource:
    """With p_markov_hold_n_mode=off the sweep passes transition_matrix=None,
    current_state="FLAT", p_markov_override=None — the engine's existing
    missing-source renormalisation must drop the source."""

    _COMMON = dict(
        trend_probs={"bull": 0.6, "bear": 0.2, "neutral": 0.2},
        direction="long",
        p_timesfm=0.6,
        tp_bricks=5,
        sl_bricks=3,
        brick_size=0.5,
    )

    def test_off_drops_markov_source(self):
        engine = EVEngine()
        result = engine.compute_ev(
            transition_matrix=None,
            current_state="FLAT",
            p_markov_override=None,
            **self._COMMON,
        )
        assert "p_markov_hold_n" not in result.sources_used
        assert set(result.sources_used) == {"p_regime", "p_timesfm"}
        # 5:5 relative weights -> 50/50
        assert result.weights_used["p_regime"] == pytest.approx(0.5)
        assert result.weights_used["p_timesfm"] == pytest.approx(0.5)

    def test_live_includes_markov_source(self):
        """Control: supplying the source puts it back in the blend — proves
        the switch, not an unconditional removal."""
        engine = EVEngine()
        matrix = {
            "UP": {"UP": 0.8, "DOWN": 0.1, "FLAT": 0.1},
            "DOWN": {"UP": 0.1, "DOWN": 0.8, "FLAT": 0.1},
            "FLAT": {"UP": 0.3, "DOWN": 0.3, "FLAT": 0.4},
        }
        result = engine.compute_ev(
            transition_matrix=matrix,
            current_state="UP",
            p_markov_override=0.75,
            **self._COMMON,
        )
        assert "p_markov_hold_n" in result.sources_used
        assert set(result.sources_used) == {"p_regime", "p_markov_hold_n", "p_timesfm"}
        # 5:4:5 over 14
        assert result.weights_used["p_markov_hold_n"] == pytest.approx(4 / 14, abs=1e-4)

    def test_weights_table_unchanged(self):
        """The retirement is a membership switch, not a weight-table edit —
        P_WIN_WEIGHTS_FULL keeps its 5:4:5 relative weights so flipping
        p_markov_hold_n_mode back to 'live' restores the prior blend
        exactly, with no migration needed.

        R2 (2026-10-23): p_router joined the table at the peer 5/14 weight;
        the three ORIGINAL entries are byte-identical, which is the
        restoration guarantee this test pins."""
        assert set(P_WIN_WEIGHTS_FULL) == {
            "p_regime", "p_markov_hold_n", "p_timesfm", "p_router",
        }
        assert P_WIN_WEIGHTS_FULL["p_regime"] == pytest.approx(5 / 14)
        assert P_WIN_WEIGHTS_FULL["p_markov_hold_n"] == pytest.approx(4 / 14)
        assert P_WIN_WEIGHTS_FULL["p_timesfm"] == pytest.approx(5 / 14)
        assert P_WIN_WEIGHTS_FULL["p_router"] == pytest.approx(5 / 14)

    def test_p_win_shifts_when_source_dropped(self):
        """Dropping a below-neutral source must RAISE p_win (it was dragging
        the logit pool down) — the direction of the live effect, asserted
        so a sign error in the wiring is caught."""
        engine = EVEngine()
        matrix = {
            "UP": {"UP": 0.2, "DOWN": 0.7, "FLAT": 0.1},
            "DOWN": {"UP": 0.1, "DOWN": 0.8, "FLAT": 0.1},
            "FLAT": {"UP": 0.3, "DOWN": 0.3, "FLAT": 0.4},
        }
        with_src = engine.compute_ev(
            transition_matrix=matrix, current_state="UP",
            p_markov_override=0.05, **self._COMMON,
        )
        without_src = engine.compute_ev(
            transition_matrix=None, current_state="FLAT",
            p_markov_override=None, **self._COMMON,
        )
        assert with_src.p_win < without_src.p_win
