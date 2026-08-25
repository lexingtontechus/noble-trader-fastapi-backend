"""
Execution Deprecation Gate
═══════════════════════════════════════════════════════════════════════════

P5 (2026-07-20) — noble-trader-fastapi-backend is no longer a trade executor.

The noble server's role is now strictly **sweep orchestration**: compute
signals, deliver qualified ones directly to Discord. Actual trade
execution lives in the hermes agent (noble-trader-agent), which routes
via env vars and connects to MT4/MT5, Hyperliquid, etc. directly.

This module provides a single gate (`assert_execution_enabled`) that every
legacy execution codepath must call before doing anything that places,
cancels, or reconciles a real order. When `NOBLE_TRADER_DISABLE_EXECUTION`
is True (the default after P5), the gate raises `ExecutionDisabledError`
which bubbles up to:

    * renko/executor.py — `AlpacaExecutor.__init__`, `execute()`, `close_position()`
    * regime_platform/services/operational/executor_factory.py — `get_executor()`
    * regime_platform/services/operational/kill_switch.py — `cancel_all_orders()`, `close_all_positions()`
    * regime_platform/services/operational/reconciler.py — `reconcile_order()`, `reconcile_all_recent()`, `reconcile_positions()`
    * regime_platform/services/operational/mode_manager.py — `_switch_executor_mode()`
    * regime_platform/routers/operational.py — HTTP routes return HTTP 410 Gone

To re-enable execution in an emergency (e.g. hermes agent down + need to
liquidate a legacy position), set `NOBLE_TRADER_DISABLE_EXECUTION=false`
in the environment. This bypass is audited via a WARNING log line.

Usage::

    from regime_platform.services.execution_deprecation import assert_execution_enabled

    def execute_order(...):
        assert_execution_enabled()  # raises ExecutionDisabledError if gated
        # ... rest of execution logic
"""

from __future__ import annotations

import logging
from typing import Optional

from regime_platform.core.settings import settings

log = logging.getLogger(__name__)


class ExecutionDisabledError(RuntimeError):
    """Raised when an execution codepath is invoked while the
    NOBLE_TRADER_DISABLE_EXECUTION gate is active.

    This is a hard error — callers MUST NOT swallow it. It indicates the
    operator attempted to use the noble server as a trade executor, which
    is no longer its role. The hermes agent (noble-trader-agent) is the
    canonical executor.

    HTTP routes that catch this should return 410 Gone with a helpful
    error body pointing operators to the hermes agent.
    """

    def __init__(self, context: Optional[str] = None):
        msg = (
            "Trade execution is disabled on noble-trader-fastapi-backend. "
            "The noble server is now a sweep orchestrator only — execution "
            "lives in the hermes agent (noble-trader-agent). "
            "To re-enable in an emergency, set NOBLE_TRADER_DISABLE_EXECUTION=false."
        )
        if context:
            msg = f"[{context}] {msg}"
        super().__init__(msg)
        self.context = context


def assert_execution_enabled(context: Optional[str] = None) -> None:
    """Raise ExecutionDisabledError if the execution gate is active.

    Parameters
    ----------
    context : str, optional
        Human-readable label for the call site (e.g. "AlpacaExecutor.__init__",
        "kill_switch.cancel_all_orders"). Included in the error message and
        log line for auditability.

    Raises
    ------
    ExecutionDisabledError
        If `settings.NOBLE_TRADER_DISABLE_EXECUTION` is True.
    """
    if settings.NOBLE_TRADER_DISABLE_EXECUTION:
        log.warning(
            "Execution gate BLOCKED — context=%s. "
            "noble-trader-fastapi-backend is not a trade executor. "
            "Use hermes agent (noble-trader-agent) for order placement.",
            context or "unknown",
        )
        raise ExecutionDisabledError(context=context)


def execution_enabled() -> bool:
    """Return True if execution codepaths are allowed to run.

    Convenience for callers that want to skip the work entirely without
    paying the cost of constructing the exception object (e.g. in tight
    loops or import-time checks).
    """
    return not settings.NOBLE_TRADER_DISABLE_EXECUTION
