"""
Operational Hardening Services — Phase 8 P0

Provides the operational scaffolding required for live trading:
- ExecutorFactory: Singleton provider for properly-configured AlpacaExecutor instances
- KillSwitchService: Three-tier emergency halt (global/user/symbol)
- AuditLogger: Append-only, immutable trade audit trail
- ModeManager: Paper/Live toggle with confirmation gates
- ReconciliationService: Fill verification and position reconciliation
"""

from regime_platform.services.operational.executor_factory import (
    executor_factory,
    ExecutorFactory,
)
from regime_platform.services.operational.kill_switch import (
    kill_switch,
    KillSwitchService,
    HaltLevel,
    HaltReason,
    HaltRecord,
)
from regime_platform.services.operational.audit_logger import (
    audit_logger,
    AuditLogger,
    AuditEventType,
    AuditEvent,
)
from regime_platform.services.operational.mode_manager import (
    mode_manager,
    ModeManager,
    TradingMode,
    ModeChangeRequest,
    ModeState,
)
from regime_platform.services.operational.reconciler import (
    reconciler,
    ReconciliationService,
    ReconciliationStatus,
    ReconciliationResult,
    PositionReconciliationResult,
)

__all__ = [
    "executor_factory", "ExecutorFactory",
    "kill_switch", "KillSwitchService", "HaltLevel", "HaltReason", "HaltRecord",
    "audit_logger", "AuditLogger", "AuditEventType", "AuditEvent",
    "mode_manager", "ModeManager", "TradingMode", "ModeChangeRequest", "ModeState",
    "reconciler", "ReconciliationService", "ReconciliationStatus",
    "ReconciliationResult", "PositionReconciliationResult",
]
