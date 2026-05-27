"""
Integration helpers - connects the Renko pipeline with existing
Noble Trader Agent modules (HMM regime, Kelly sizing, risk analysis).

Usage from the deployed FastAPI backend::

    from renko.integration import NobleTraderRenkoBridge

    bridge = NobleTraderRenkoBridge(fastapi_base="https://noble-trader-fastapi-backend.onrender.com")
    regime = await bridge.fetch_regime(symbol="SPY")
    bridge.pipeline.set_regime(regime)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from renko.config import RenkoConfig
from renko.pipeline import RenkoPipeline
from regime_platform.core.circuit_breaker import circuit, CircuitOpenError

logger = logging.getLogger(__name__)


class NobleTraderRenkoBridge:
    """
    Bridge between the Renko HFT pipeline and the existing
    Noble Trader Agent infrastructure.

    Responsibilities:
      - Fetch HMM regime from FastAPI backend
      - Fetch Kelly sizing from FastAPI backend
      - Push Renko trade results to Supabase
      - Provide pipeline config to Strategy Evolution (GA)
    """

    def __init__(
        self,
        fastapi_base: str = "http://localhost:8000",
        fastapi_user: Optional[str] = None,
        fastapi_password: Optional[str] = None,
        fastapi_api_key: Optional[str] = None,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
        config: Optional[RenkoConfig] = None,
    ):
        self.fastapi_base = fastapi_base.rstrip("/")
        self.fastapi_user = fastapi_user
        self.fastapi_password = fastapi_password
        self.fastapi_api_key = fastapi_api_key
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key

        self.pipeline = RenkoPipeline(config)
        self._auth_token: Optional[str] = None
        self._supabase_warning_logged: bool = False

    # ── Regime Integration ────────────────────────────────────────

    async def fetch_regime(self, symbol: str) -> Optional[str]:
        """
        Fetch the current HMM regime from the FastAPI backend.

        Calls ``/regime/detect`` with recent price data and returns
        the regime name for the Signal Filter's regime gate.
        """
        cb = circuit("fastapi_regime", failure_threshold=5, reset_timeout=30.0)

        try:
            import httpx

            if cb.is_open:
                logger.warning(f"Circuit [fastapi_regime] OPEN - skipping regime fetch")
                return None

            headers = await self._get_headers()

            # Get recent bricks to use as price data
            bricks = list(self.pipeline.brick_engine.history)
            if len(bricks) < 20:
                return None

            prices = [b.close_price for b in bricks[-50:]]

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.fastapi_base}/regime/detect",
                    json={"symbol": symbol, "prices": prices},
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    regime_name = data.get("regime_name", data.get("regime", None))
                    if regime_name:
                        self.pipeline.set_regime(regime_name)
                    cb._on_success()
                    return regime_name
                else:
                    cb._on_failure(RuntimeError(f"HTTP {resp.status_code}"))

        except ImportError:
            logger.warning("httpx not installed - cannot fetch regime")
        except Exception as e:
            cb._on_failure(e)
            logger.error(f"Regime fetch failed: {e}")

        return None

    # ── Kelly Integration ─────────────────────────────────────────

    async def fetch_kelly(self, symbol: str) -> Optional[Dict]:
        """
        Fetch Kelly position sizing from the FastAPI backend.

        Calls ``/size/kelly`` with win rate and risk/reward data
        derived from the trade journal.
        """
        cb = circuit("fastapi_kelly", failure_threshold=5, reset_timeout=30.0)

        try:
            import httpx

            if cb.is_open:
                logger.warning(f"Circuit [fastapi_kelly] OPEN - skipping Kelly fetch")
                return None

            headers = await self._get_headers()
            stats = self.pipeline.journal.get_stats(last_n=50)

            if "kelly_inputs" not in stats:
                return None

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.fastapi_base}/size/kelly",
                    json={
                        "symbol": symbol,
                        "win_rate": stats["kelly_inputs"]["win_rate"],
                        "avg_win": stats["kelly_inputs"]["avg_win"],
                        "avg_loss": stats["kelly_inputs"]["avg_loss"],
                    },
                    headers=headers,
                )
                if resp.status_code == 200:
                    cb._on_success()
                    return resp.json()
                else:
                    cb._on_failure(RuntimeError(f"HTTP {resp.status_code}"))

        except ImportError:
            logger.warning("httpx not installed - cannot fetch Kelly")
        except Exception as e:
            cb._on_failure(e)
            logger.error(f"Kelly fetch failed: {e}")

        return None

    # ── Supabase Integration ──────────────────────────────────────

    async def push_trade_to_supabase(self, trade_data: Dict) -> bool:
        """
        Push a completed trade to the Supabase ``ta_trade_recommendation``
        table for persistence and dashboard display.
        """
        if not self.supabase_url or not self.supabase_key:
            if not self._supabase_warning_logged:
                logger.warning(
                    "Supabase not configured - trade data not persisted. "
                    "Set SUPABASE_URL and SUPABASE_KEY env vars."
                )
                self._supabase_warning_logged = True
            return False

        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.supabase_url}/rest/v1/ta_trade_recommendation",
                    json=trade_data,
                    headers={
                        "apikey": self.supabase_key,
                        "Authorization": f"Bearer {self.supabase_key}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal",
                    },
                )
                if resp.status_code in (200, 201):
                    return True
                else:
                    logger.error(
                        "Supabase push failed: status=%s body=%s",
                        resp.status_code,
                        resp.text,
                    )
                    return False

        except Exception as e:
            logger.error(
                "Supabase push failed: %s - trade_data keys=%s",
                e,
                list(trade_data.keys()) if isinstance(trade_data, dict) else "N/A",
            )
            return False

    # ── Strategy Evolution Integration ────────────────────────────

    def get_evolvable_params(self) -> Dict[str, Any]:
        """
        Get pipeline parameters that can be optimised by the
        Strategy Evolution GA module.

        Returns a dict of parameter names, current values, and
        search bounds.
        """
        config = self.pipeline.config
        return {
            "brick_size": {"value": config.brick_size, "min": 0.05, "max": 2.0, "type": "float"},
            "bull_trigger_n": {"value": config.bull_trigger_n, "min": 2, "max": 8, "type": "int"},
            "bear_trigger_n": {"value": config.bear_trigger_n, "min": 2, "max": 8, "type": "int"},
            "sl_bricks": {"value": config.sl_bricks, "min": 1, "max": 6, "type": "int"},
            "tp_bricks": {"value": config.tp_bricks, "min": 2, "max": 10, "type": "int"},
            "trail_after_bricks": {"value": config.trail_after_bricks, "min": 1, "max": 6, "type": "int"},
            "trail_distance_bricks": {"value": config.trail_distance_bricks, "min": 1, "max": 4, "type": "int"},
            "max_trades_per_session": {"value": config.max_trades_per_session, "min": 5, "max": 30, "type": "int"},
            "cooldown_seconds": {"value": config.cooldown_seconds, "min": 5.0, "max": 120.0, "type": "float"},
        }

    def apply_evolved_params(self, params: Dict[str, Any]) -> None:
        """
        Apply parameters from the Strategy Evolution GA to the pipeline.

        Creates a new RenkoConfig with the evolved parameters and
        reinitialises the pipeline.
        """
        config = self.pipeline.config

        for key, value in params.items():
            if hasattr(config, key):
                setattr(config, key, value)

        # Rebuild pipeline with new config
        self.pipeline = RenkoPipeline(config)

    # ── Auth ──────────────────────────────────────────────────────

    async def _get_headers(self) -> Dict[str, str]:
        """Get auth headers for FastAPI requests."""
        headers = {"Content-Type": "application/json"}

        if self.fastapi_api_key:
            headers["X-API-Key"] = self.fastapi_api_key
        elif self.fastapi_user and self.fastapi_password:
            token = await self._authenticate()
            if token:
                headers["Authorization"] = f"Bearer {token}"

        return headers

    async def _authenticate(self) -> Optional[str]:
        """Authenticate with the FastAPI backend."""
        if self._auth_token:
            return self._auth_token

        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.fastapi_base}/auth/token",
                    data={
                        "username": self.fastapi_user,
                        "password": self.fastapi_password,
                    },
                )
                if resp.status_code == 200:
                    self._auth_token = resp.json().get("access_token")
                    return self._auth_token
        except Exception as e:
            logger.error(f"Auth failed: {e}")

        return None
