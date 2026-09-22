"""A flipped live; B light geometry gate; C macro edge stamp tests."""

from __future__ import annotations

import pytest

from regime_platform.core.feature_flags import (
    get_geometry_intraday_scale_mode,
    get_geometry_tuning_sweep_mode,
    get_light_publish_geometry_gate,
)


class TestFlagsAfterFlip:
    def test_geometry_modes_live(self):
        assert get_geometry_intraday_scale_mode() == "live"
        assert get_geometry_tuning_sweep_mode() == "live"
        assert get_light_publish_geometry_gate() == "shadow"


class TestLightGateLogic:
    def test_would_reject_rules(self):
        # mirror orchestrator light gate math
        def would(risk, p_br, cap=0.35, min_br=0.20):
            risk_over = risk is not None and risk > cap
            br_reject = p_br is not None and p_br < min_br
            return risk_over or br_reject

        assert would(None, None) is False  # fail-open
        assert would(1.0, None) is True
        assert would(None, 0.1) is True
        assert would(0.1, 0.8) is False
        assert would(0.4, 0.1) is True


class TestMacroEdgeStamp:
    def test_computed_at_in_allowlist(self):
        from regime_platform.sweep_orchestrator.supabase_client import (
            NT_MACRO_EDGE_BY_SYMBOL_COLUMNS,
        )
        assert "computed_at" in NT_MACRO_EDGE_BY_SYMBOL_COLUMNS

    def test_build_stamps_computed_at(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from regime_platform.sweep_orchestrator import macro_edge as me

        rows = [{
            "symbol": "EURUSD", "signal": "buy", "outcome": "tp_hit",
            "resolved_at": "2026-09-01T10:00:00+00:00",
            "ts": "2026-09-01T00:00:00+00:00",
            "macro_label": "bull", "tp_bricks": 6, "sl_bricks": 3,
        }]

        async def fake_fetch(*a, **k):
            return rows

        captured = []

        async def fake_upsert(out):
            captured.extend(out)

        with (
            patch.object(me, "_fetch_all", new=AsyncMock(side_effect=fake_fetch)),
            patch.object(me, "upsert_macro_edge_by_symbol", new=AsyncMock(side_effect=fake_upsert)),
        ):
            out = asyncio.run(me.build_macro_edge_table())
        assert out and out[0].get("computed_at")
