# Phase 8: Operational Hardening — From Research Platform to Live-Tradable System

> **Status:** SCOPED — Ready for implementation  
> **Prerequisite:** Phases 1-7 COMPLETE (HMM regime engine, Renko pipeline, walk-forward, Monte Carlo, statistical rigor, execution modeling)  
> **Goal:** Bridge the gap between "impressive quant engine" and "production trading system" by building the operational scaffolding that institutions require before deploying real capital.

---

## 0. Why Phase 8 Exists

Phases 1-7 built a **Ferrari engine on a go-kart chassis.** The quantitative toolkit — HMM regime detection, Renko signal pipeline with 7-layer filtering, walk-forward validation, bootstrap CI, deflated Sharpe, Almgren-Chriss execution modeling, strategy evolution with A/B testing — is genuinely institutional-grade. In several areas (integrated regime-aware execution, TDA persistent homology, automated strategy rotation), it exceeds what most sell-side desks have in production.

What's missing is the **operational infrastructure** — the unglamorous but essential pieces that separate a working research system from a system you'd trust with real money. Institutions call this "operational readiness," and it has hard requirements: kill switches, immutable audit trails, fill reconciliation, paper/live toggle with confirmation gates, multi-tenant isolation, and per-feature RBAC. Without these, no compliance officer signs off, no risk manager green-lights, and no PM trades live.

Phase 8 is deliberately scoped in priority tiers so you can ship incrementally. P0 items are the absolute minimum for live trading. P1 items make it production-hardened. P2 items make it institution-grade.

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        FRONTEND (Next.js)                           │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌─────────┐│
│  │ Kill     │ │ Paper/   │ │ Real-time │ │ Audit    │ │ Settings││
│  │ Switch   │ │ Live     │ │ P&L       │ │ Log      │ │ Page    ││
│  │ Panel    │ │ Toggle   │ │ Dashboard │ │ Viewer   │ │         ││
│  └────┬─────┘ └────┬─────┘ └─────┬─────┘ └────┬─────┘ └────┬────┘│
│       │            │             │             │             │      │
│  ┌────▼────────────▼─────────────▼─────────────▼─────────────▼────┐│
│  │                    BFF Proxy Layer                              ││
│  │  /api/trading/kill-switch  /api/trading/mode                  ││
│  │  /api/trading/audit-log   /api/trading/reconcile              ││
│  │  /api/settings/*          /api/auth/rbac                      ││
│  └────────────────────────────┬───────────────────────────────────┘│
└───────────────────────────────┼─────────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────────┐
│                     BACKEND (FastAPI)                                │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────┐│
│  │ Kill Switch  │ │ Mode Manager │ │ Audit Logger │ │ Reconciler ││
│  │ Service      │ │ (Paper/Live) │ │ (Append-Only │ │ (Fill      ││
│  │              │ │              │ │  DB Table)   │ │  Verify)   ││
│  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────┬─────┘│
│         │                │                │                │       │
│  ┌──────▼────────────────▼────────────────▼────────────────▼─────┐│
│  │              Operational Middleware Layer                       ││
│  │  RBAC Enforcer │ Rate Limiter │ Kill Switch Gate │ Audit Trail ││
│  └───────────────────────────────────────────────────────────────┘│
│         │                │                │                │       │
│  ┌──────▼────────────────▼────────────────▼────────────────▼─────┐│
│  │              AlpacaExecutor + Signal Filter + Risk Manager     ││
│  └───────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │   Alpaca Broker API   │
                    │  (Paper or Live)      │
                    └───────────────────────┘
```

---

## 2. P0 — Live Trading Blockers (Must Build First)

Estimated total: **7-10 days**

### 2A. Kill Switch / Emergency Halt

**Problem:** If something goes wrong — a bug in the signal pipeline, a data feed error, a runaway algo — there is currently no way to immediately stop all trading activity. The circuit breaker only protects individual API calls, not the overall trading system.

**Solution:** A three-tier kill switch system with frontend UI, backend service, and middleware enforcement.

#### Backend: Kill Switch Service

**New file:** `regime_platform/services/operational/kill_switch.py`

```python
# Three-tier kill switch:
# 1. GLOBAL_HALT — stops ALL trading across all users, all symbols
# 2. USER_HALT   — stops trading for a specific user
# 3. SYMBOL_HALT — stops trading for a specific symbol

from enum import Enum
from datetime import datetime
from pydantic import BaseModel
from typing import Optional

class HaltLevel(str, Enum):
    GLOBAL_HALT = "global_halt"      # Nuclear option
    USER_HALT = "user_halt"          # Per-user
    SYMBOL_HALT = "symbol_halt"      # Per-symbol

class HaltReason(str, Enum):
    MANUAL = "manual"                # Human triggered
    CIRCUIT_BREAKER = "circuit_breaker"
    MAX_DRAWDOWN = "max_drawdown"    # Auto-triggered
    DATA_FEED_ERROR = "data_feed_error"
    COMPLIANCE = "compliance"

class HaltRecord(BaseModel):
    level: HaltLevel
    reason: HaltReason
    triggered_by: str                # user_id or "system"
    triggered_at: datetime
    scope: Optional[str] = None      # user_id or symbol
    notes: Optional[str] = None

class KillSwitchService:
    """
    In-memory + Redis-backed kill switch state.
    - Redis for cross-process persistence (multiple uvicorn workers)
    - In-memory cache for zero-latency checks in the hot path
    """

    HALT_KEY = "noble:halt:global"
    USER_HALT_KEY = "noble:halt:user:{user_id}"
    SYMBOL_HALT_KEY = "noble:halt:symbol:{symbol}"

    async def activate_halt(self, record: HaltRecord) -> None: ...
    async def deactivate_halt(self, level: HaltLevel, scope: Optional[str] = None) -> None: ...
    async def is_halted(self, level: HaltLevel = HaltLevel.GLOBAL_HALT, scope: Optional[str] = None) -> bool: ...
    async def get_active_halts(self) -> list[HaltRecord]: ...

    # Emergency actions
    async def cancel_all_orders(self, user_id: str) -> list[str]: ...
    async def close_all_positions(self, user_id: str) -> list[str]: ...
```

#### Backend: Kill Switch Middleware

**New file:** `regime_platform/middleware/kill_switch.py`

```python
# FastAPI middleware that checks kill switch BEFORE any order submission
# This sits between the request handler and AlpacaExecutor

class KillSwitchMiddleware:
    """
    Intercepts ALL requests to /trading/execute, /alpaca/orders/create,
    /renko/orders, /trading/approve — returns 403 with halt details
    if an active halt exists.
    """
    PROTECTED_PATHS = [
        "/trading/execute",
        "/trading/approve",
        "/alpaca/orders/create",
        "/renko/orders",
    ]

    async def dispatch(self, request, call_next):
        if any(request.url.path.startswith(p) for p in self.PROTECTED_PATHS):
            kill_switch = request.app.state.kill_switch
            if await kill_switch.is_halted():
                active = await kill_switch.get_active_halts()
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "TRADING_HALTED",
                        "halts": [h.model_dump() for h in active],
                        "message": "All trading is halted. Use kill switch panel to review."
                    }
                )
        return await call_next(request)
```

#### Frontend: Kill Switch Panel

**New file:** `src/components/operational/KillSwitchPanel.jsx`

```
┌─────────────────────────────────────────────────────┐
│ ⚠️ EMERGENCY CONTROLS                    [ADMIN]    │
├─────────────────────────────────────────────────────┤
│                                                     │
│  ┌─────────────────────────────────────────────┐    │
│  │  🔴 GLOBAL HALT                            │    │
│  │  Stop ALL trading across all users/symbols  │    │
│  │  [ACTIVATE GLOBAL HALT]  ← requires 2-click │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  ┌─────────────────────────────────────────────┐    │
│  │  🟡 USER HALT                              │    │
│  │  User: [dropdown]  Reason: [dropdown]       │    │
│  │  [HALT USER TRADING]                        │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  ┌─────────────────────────────────────────────┐    │
│  │  🟠 SYMBOL HALT                            │    │
│  │  Symbol: [input]  Reason: [dropdown]        │    │
│  │  [HALT SYMBOL TRADING]                      │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  ── Emergency Actions ──                            │
│  [Cancel All Open Orders]  [Close All Positions]    │
│                                                     │
│  ── Active Halts ──                                 │
│  🔴 GLOBAL — Manual — admin@noble — 2m ago         │
│     [DEACTIVATE]                                    │
│                                                     │
└─────────────────────────────────────────────────────┘
```

**DaisyUI components:** `card`, `badge`, `btn btn-error`, `select`, `alert alert-warning`

**BFF routes:**
- `GET /api/trading/kill-switch/status` — active halts
- `POST /api/trading/kill-switch/activate` — activate halt (requires admin role)
- `POST /api/trading/kill-switch/deactivate` — deactivate halt (requires admin role)
- `POST /api/trading/kill-switch/cancel-all` — cancel all orders
- `POST /api/trading/kill-switch/close-all` — close all positions

**Estimated effort:** 2 days

---

### 2B. Persistent Audit Log (Append-Only Trade Ledger)

**Problem:** The current `TradeJournal` is an in-memory deque (max 10,000 entries) that's lost on restart. The `executor.py` order history is also in-memory. There is no immutable, append-only record of what was executed, when, and why. This is a compliance non-negotiable — without an audit trail, you can't reconcile, can't investigate incidents, and can't satisfy any regulatory requirement.

**Solution:** PostgreSQL append-only audit table + structured event logging.

#### Database Schema

**New migration:** Supabase SQL

```sql
-- Append-only trade audit log
-- No UPDATE or DELETE permissions — INSERT and SELECT only
CREATE TABLE trade_audit_log (
    id            BIGSERIAL PRIMARY KEY,
    event_type    VARCHAR(50) NOT NULL,     -- ORDER_SUBMITTED, ORDER_FILLED, ORDER_REJECTED,
                                             -- ORDER_CANCELLED, HALT_ACTIVATED, HALT_DEACTIVATED,
                                             -- SIGNAL_GENERATED, SIGNAL_FILTERED, APPROVAL_GRANTED,
                                             -- APPROVAL_REVOKED, RECONCILIATION_PASSED,
                                             -- RECONCILIATION_FAILED
    user_id       VARCHAR(255) NOT NULL,
    symbol        VARCHAR(20),
    order_id      VARCHAR(100),             -- Alpaca order ID
    direction     VARCHAR(10),              -- buy/sell
    quantity      DECIMAL(18, 4),
    price         DECIMAL(18, 4),
    order_type    VARCHAR(20),              -- market/limit/stop/stop_limit/trailing_stop
    regime        VARCHAR(20),              -- HMM regime at time of event
    strategy      VARCHAR(50),              -- Which strategy variant generated this
    signal_score  DECIMAL(5, 4),            -- Composite score from validation
    risk_metrics  JSONB,                    -- Snapshot of risk state at event time
    metadata      JSONB,                    -- Additional context (fill price, slippage, etc.)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Prevent any modifications
    CONSTRAINT no_update_or_delete CHECK (true)  -- Enforced via RLS policy
);

-- Row-Level Security: INSERT only (no UPDATE/DELETE)
ALTER TABLE trade_audit_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can insert audit events" ON trade_audit_log
    FOR INSERT WITH CHECK (auth.uid() IS NOT NULL);

CREATE POLICY "Users can read own audit events" ON trade_audit_log
    FOR SELECT USING (user_id = auth.uid());

CREATE POLICY "Admins can read all audit events" ON trade_audit_log
    FOR SELECT USING (
        EXISTS (SELECT 1 FROM auth.users WHERE auth.uid() = id AND raw_user_meta_data->>'role' = 'admin')
    );

-- Indexes for common queries
CREATE INDEX idx_audit_user_id ON trade_audit_log (user_id);
CREATE INDEX idx_audit_symbol ON trade_audit_log (symbol);
CREATE INDEX idx_audit_event_type ON trade_audit_log (event_type);
CREATE INDEX idx_audit_created_at ON trade_audit_log (created_at DESC);
CREATE INDEX idx_audit_order_id ON trade_audit_log (order_id);
```

#### Backend: Audit Logger Service

**New file:** `regime_platform/services/operational/audit_logger.py`

```python
from datetime import datetime
from typing import Optional
from enum import Enum

class AuditEventType(str, Enum):
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    SIGNAL_FILTERED = "SIGNAL_FILTERED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REVOKED = "APPROVAL_REVOKED"
    HALT_ACTIVATED = "HALT_ACTIVATED"
    HALT_DEACTIVATED = "HALT_DEACTIVATED"
    RECONCILIATION_PASSED = "RECONCILIATION_PASSED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    MODE_CHANGED = "MODE_CHANGED"         # Paper ↔ Live
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    RISK_LIMIT_BREACH = "RISK_LIMIT_BREACH"

class AuditLogger:
    """
    Append-only audit logger. Every write goes to:
    1. PostgreSQL trade_audit_log (source of truth)
    2. Python structlog (stdout/CloudWatch for real-time monitoring)
    3. Discord webhook (critical events only: halts, risk breaches, reconciliations)

    This service is called by:
    - AlpacaExecutor after every order submission/fill
    - KillSwitchService on activate/deactivate
    - TradingWorkflow on approve/reject
    - ReconciliationService on fill verification
    - ModeManager on paper/live switch
    """

    async def log_event(
        self,
        event_type: AuditEventType,
        user_id: str,
        symbol: Optional[str] = None,
        order_id: Optional[str] = None,
        direction: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        order_type: Optional[str] = None,
        regime: Optional[str] = None,
        strategy: Optional[str] = None,
        signal_score: Optional[float] = None,
        risk_metrics: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """Returns the audit log ID. Raises on failure — audit events must never be silently lost."""
        ...
```

#### Frontend: Audit Log Viewer

**New file:** `src/components/operational/AuditLogViewer.jsx`

```
┌──────────────────────────────────────────────────────────────────┐
│ Trade Audit Log                                     [Export CSV] │
├──────────────────────────────────────────────────────────────────┤
│ Filters: [Event Type ▼] [Symbol ▼] [Date Range] [User ▼]      │
│                                                                  │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ ORDER_FILLED │ AAPL │ buy 100 @ $187.42 │ Regime: bullish  │ │
│ │ Order: abc-123 │ Strategy: renko_atr_2 │ Score: 0.82      │ │
│ │ 2025-01-15 09:31:22 UTC │ admin@noble                     │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ HALT_ACTIVATED │ GLOBAL │ Manual │ admin@noble              │ │
│ │ 2025-01-15 10:15:03 UTC │ Reason: data_feed_error          │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ ...                                                              │
│                                                                  │
│ Page 1 of 47  [< Prev] [1] [2] [3] ... [47] [Next >]          │
└──────────────────────────────────────────────────────────────────┘
```

**DaisyUI components:** `card`, `badge` (color-coded by event type), `select`, `btn`, `table`

**BFF routes:**
- `GET /api/trading/audit-log` — paginated query with filters
- `GET /api/trading/audit-log/export` — CSV export
- `GET /api/trading/audit-log/summary` — event counts by type for last 24h/7d/30d

**Estimated effort:** 2-3 days

---

### 2C. Paper/Live Mode Toggle with Confirmation Gate

**Problem:** The frontend is hardcoded to `ALPACA_PAPER_BASE_URL`. The backend infers mode from the URL string. There's no UI toggle, no confirmation flow, no visual indicator of which mode you're in, and no audit trail of mode switches. An accidental URL change could route real money orders.

**Solution:** Explicit mode management with multi-gate confirmation, visual indicators, and full audit trail.

#### Backend: Mode Manager Service

**New file:** `regime_platform/services/operational/mode_manager.py`

```python
class TradingMode(str, Enum):
    SIMULATION = "simulation"   # No API keys — orders logged locally
    PAPER = "paper"             # Alpaca paper API
    LIVE = "live"               # Alpaca live API — REAL MONEY

class ModeManager:
    """
    Manages paper/live mode with:
    1. Redis-backed mode state (persists across restarts)
    2. Mode switch requires confirmation token (prevents accidental switches)
    3. Every mode change is audit-logged
    4. Mode change triggers health check on target environment
    5. Live mode requires explicit acknowledgment of risk disclaimer

    Flow for PAPER → LIVE:
    1. User requests mode change in UI
    2. Backend generates confirmation token, stores in Redis with 5-min TTL
    3. UI shows risk disclaimer + confirmation dialog with token
    4. User confirms (must type "I UNDERSTAND THE RISKS")
    5. Backend validates confirmation token
    6. Backend switches AlpacaExecutor base_url to live endpoint
    7. Backend logs MODE_CHANGED event to audit trail
    8. Backend sends Discord notification
    9. Frontend updates all visual indicators

    Flow for LIVE → PAPER:
    1. User requests mode change
    2. Backend cancels all open live orders (safety)
    3. Backend switches to paper endpoint
    4. Audit log + Discord notification
    """

    async def request_mode_change(self, target_mode: TradingMode, user_id: str) -> str:
        """Returns confirmation token."""

    async def confirm_mode_change(self, token: str, user_id: str, acknowledgment: str) -> TradingMode:
        """Validates and executes mode change. Raises on invalid/expired token."""

    async def get_current_mode(self) -> TradingMode: ...
```

#### Frontend: Mode Toggle + Confirmation Flow

**New file:** `src/components/operational/ModeToggle.jsx`

```
┌─────────────────────────────────────────────────────────┐
│  Current Mode:  🟢 PAPER TRADING                       │
│                                                         │
│  Switch to: [🔴 LIVE TRADING]                           │
│                                                         │
└─────────────────────────────────────────────────────────┘

After clicking "LIVE TRADING":

┌─────────────────────────────────────────────────────────┐
│  ⚠️  LIVE TRADING MODE ACTIVATION                       │
│                                                         │
│  You are about to switch to LIVE TRADING.               │
│  Real money will be at risk.                            │
│                                                         │
│  □ I have tested my strategy in paper mode              │
│  □ I understand that losses are real and irreversible   │
│  □ I have set appropriate risk limits                   │
│  □ I have verified my Alpaca live API keys              │
│                                                         │
│  Type "I UNDERSTAND THE RISKS" to confirm:              │
│  [________________________________________]             │
│                                                         │
│  [CANCEL]                      [ACTIVATE LIVE TRADING]  │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Navbar indicator:** When in live mode, the navbar shows a persistent `🔴 LIVE` badge that pulses red. The entire page gets a subtle red border. The footer changes from "Paper Trading Only" to "⚠️ LIVE TRADING — Real Money at Risk".

**BFF routes:**
- `GET /api/trading/mode` — current mode
- `POST /api/trading/mode/request` — request mode change (returns confirmation token)
- `POST /api/trading/mode/confirm` — confirm mode change

**Config changes:**
- Add `NEXT_PUBLIC_ALPACA_LIVE_BASE_URL` env var
- Add `ALPACA_LIVE_BASE_URL` to backend `settings.py`
- `alpaca-client.js` reads mode from `/api/trading/mode` and uses appropriate URL

**Estimated effort:** 1.5-2 days

---

### 2D. Fill Reconciliation

**Problem:** After submitting orders to Alpaca, there is no verification that orders were actually filled at the expected prices. A failed fill, partial fill, or fill at a vastly different price could go undetected, leading to incorrect position tracking and phantom P&L.

**Solution:** Periodic reconciliation service that compares internal order expectations against Alpaca's reported fills.

#### Backend: Reconciliation Service

**New file:** `regime_platform/services/operational/reconciler.py`

```python
class ReconciliationResult(BaseModel):
    order_id: str
    expected_price: Optional[float]
    actual_fill_price: Optional[float]
    expected_quantity: Optional[float]
    actual_filled_quantity: Optional[float]
    slippage_bps: Optional[float]        # (actual - expected) / expected * 10000
    status: str                           # MATCHED, PARTIAL_FILL, MISSED_FILL, PRICE_DEVIATION
    discrepancy_notes: Optional[str]

class ReconciliationService:
    """
    Runs reconciliation on two schedules:
    1. IMMEDIATE — After every order submission, poll Alpaca for fill status
       (current OrderTracker does this, but doesn't compare against expectations)
    2. PERIODIC — Every 15 minutes, reconcile all orders from the last 24 hours

    Thresholds (configurable):
    - SLIPPAGE_WARNING: > 50 bps → log warning
    - SLIPPAGE_CRITICAL: > 200 bps → trigger risk alert + Discord notification
    - PARTIAL_FILL_TIMEOUT: 5 minutes → convert remaining to market order or cancel
    - MISSED_FILL_TIMEOUT: 10 minutes → escalate to manual review

    Also reconciles position counts: our DB positions vs Alpaca's reported positions.
    """

    async def reconcile_order(self, order_id: str) -> ReconciliationResult: ...
    async def reconcile_all_recent(self) -> list[ReconciliationResult]: ...
    async def reconcile_positions(self, user_id: str) -> dict: ...
    async def handle_discrepancy(self, result: ReconciliationResult) -> None: ...
```

#### Frontend: Reconciliation Status Panel

**New file:** `src/components/operational/ReconciliationPanel.jsx`

```
┌──────────────────────────────────────────────────────────┐
│ Fill Reconciliation                      [Run Now]       │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  Last reconciliation: 2025-01-15 14:30:00 UTC            │
│  Result: ✅ 47 matched | ⚠️ 2 warnings | ❌ 0 critical  │
│                                                          │
│  ┌─ Warnings ────────────────────────────────────────┐   │
│  │ AAPL order abc-123: slippage 67 bps              │   │
│  │ Expected $187.42, filled $187.55                  │   │
│  └───────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ Position Reconciliation ─────────────────────────┐   │
│  │ Our DB: 5 positions  │  Alpaca: 5 positions      │   │
│  │ Status: ✅ MATCH                                 │   │
│  └───────────────────────────────────────────────────┘   │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

**BFF routes:**
- `GET /api/trading/reconcile/status` — last reconciliation results
- `POST /api/trading/reconcile/run` — trigger manual reconciliation
- `GET /api/trading/reconcile/positions` — position comparison

**Estimated effort:** 2-3 days

---

## 3. P1 — Production Hardening (Should Build Next)

Estimated total: **5-8 days**

### 3A. Multi-Tenant Isolation

**Problem:** All database queries use `userId: "default"` hardcoded. Every user sees the same data, the same backtests, the same trade recommendations. In production, each user must only see their own data.

**Solution:** Replace all `userId: "default"` with the actual Clerk user ID from the JWT token.

**Files to modify:**
- All BFF route handlers that pass `userId` to the backend
- Backend services that accept `user_id` parameter
- Supabase RLS policies must enforce `user_id = auth.uid()`

**Implementation:**

1. Create a `useAuth()` hook that extracts `userId` from Clerk session:
   ```jsx
   // src/hooks/useAuth.js
   import { useAuth as useClerkAuth } from '@clerk/nextjs';
   export function useAuth() {
     const { userId, isSignedIn, getToken } = useClerkAuth();
     return { userId: isSignedIn ? userId : null, isSignedIn, getToken };
   }
   ```

2. All BFF fetch calls include `userId` in the request body or as a query parameter
3. Backend middleware validates `user_id` matches the JWT `sub` claim (prevents spoofing)
4. Supabase queries add `WHERE user_id = $1` everywhere

**Estimated effort:** 2-3 days (mostly find-and-replace + testing)

---

### 3B. Alpaca Order Rate Throttle

**Problem:** No explicit rate limiting for Alpaca order submissions. A burst of signals from the Renko pipeline could hit Alpaca's 200 req/min limit, causing rejected orders and potential missed fills.

**Solution:** Token bucket rate limiter specifically for Alpaca API calls.

```python
# regime_platform/middleware/alpaca_rate_limiter.py
class AlpacaRateLimiter:
    """
    Token bucket rate limiter for Alpaca API calls.
    - 200 requests/minute (Alpaca limit)
    - Burst: up to 10 requests in 1 second
    - Separate buckets for order submission vs data queries
    """
    ORDER_RATE = 30          # orders per minute (conservative, well under 200)
    ORDER_BURST = 5          # max burst
    QUERY_RATE = 120         # queries per minute
    QUERY_BURST = 20         # max burst
```

**Estimated effort:** 0.5 days

---

### 3C. Server-Side Auth Middleware (proxy.ts)

**Problem:** Auth protection is client-side only (Clerk `<Show>` component). Any signed-in user can access all API routes, including admin functions and order execution. The `proxy.ts` file forwards requests without checking roles.

**Solution:** Add Clerk session validation + role checking in `proxy.ts`.

```typescript
// src/app/api/[...path]/proxy.ts — enhanced auth middleware

async function validateRequest(request: Request) {
  const session = await auth();

  if (!session?.userId) {
    return new Response('Unauthorized', { status: 401 });
  }

  // Role-based access
  const role = session.sessionClaims?.metadata?.role;
  const path = new URL(request.url).pathname;

  // Admin-only routes
  if (path.startsWith('/api/trading/kill-switch') && role !== 'admin') {
    return new Response('Forbidden', { status: 403 });
  }

  // Trader-only routes (order execution)
  if (path.startsWith('/api/trading/execute') && !['admin', 'trader'].includes(role)) {
    return new Response('Forbidden', { status: 403 });
  }

  return null; // Continue to handler
}
```

**Estimated effort:** 1 day

---

### 3D. Real-Time P&L Dashboard

**Problem:** Positions and P&L are fetched on-demand only. A PM watching live positions has no streaming updates — they must manually refresh to see current P&L. In a live trading context, stale P&L means missed stop-outs and delayed risk decisions.

**Solution:** SSE-based position P&L streaming with Alpaca's position snapshots.

**New component:** `src/components/operational/LivePLDashboard.jsx`

```
┌─────────────────────────────────────────────────────────────────┐
│ Live P&L Dashboard              Last update: 2s ago  🟢 LIVE   │
├──────────┬──────────┬───────────┬──────────┬──────────┬────────┤
│ Symbol   │ Qty      │ Entry     │ Current  │ P&L $    │ P&L %  │
├──────────┼──────────┼───────────┼──────────┼──────────┼────────┤
│ AAPL     │ +100     │ $185.20   │ $187.42  │ +$222.00 │ +1.20% │
│ TSLA     │ +50      │ $248.10   │ $244.80  │ -$165.00 │ -1.33% │
│ GOOGL    │ +75      │ $142.30   │ $143.85  │ +$116.25 │ +1.09% │
├──────────┼──────────┼───────────┼──────────┼──────────┤
│ TOTAL    │          │           │          │ +$173.25 │ +0.32% │
└──────────┴──────────┴───────────┴──────────┴──────────┴────────┘
│  Portfolio: $104,273.25 │ Buying Power: $45,120 │ Day P&L: +$173 │
└─────────────────────────────────────────────────────────────────┘
```

**Implementation approach:**
- Poll Alpaca positions every 5 seconds via `/api/alpaca/positions`
- Use React `useSWR` with `refreshInterval: 5000` for automatic polling
- Show stale indicator if last update > 15 seconds
- Color-code: green for gains, red for losses, flashing for large moves (>1%)

**Estimated effort:** 2-3 days

---

### 3E. Per-Feature RBAC in Frontend

**Problem:** Only the Admin page checks user roles. Any signed-in user (including `viewer` role) can navigate to the Orders tab and place trades.

**Solution:** Create a `useRole()` hook and a `<RoleGate>` component.

```jsx
// src/hooks/useRole.js
export function useRole() {
  const { sessionClaims } = useAuth();
  const role = sessionClaims?.metadata?.role || 'viewer';
  return {
    role,
    isAdmin: role === 'admin',
    isTrader: ['admin', 'trader'].includes(role),
    isViewer: role === 'viewer',
  };
}

// src/components/shared/RoleGate.jsx
export function RoleGate({ minRole, children, fallback = null }) {
  const { role } = useRole();
  const hierarchy = { viewer: 0, trader: 1, admin: 2 };
  return hierarchy[role] >= hierarchy[minRole] ? children : fallback;
}
```

**Usage:**
```jsx
<RoleGate minRole="trader">
  <button onClick={executeTrade}>Execute Trade</button>
</RoleGate>
<RoleGate minRole="admin">
  <KillSwitchPanel />
</RoleGate>
```

**Estimated effort:** 1 day

---

## 4. P2 — Institution-Grade Enhancements (Nice-to-Have)

Estimated total: **8-12 days**

### 4A. Compliance Reporting Module

**Problem:** No regulatory reporting capability. Institutions must generate trade blotters, transaction reports, and risk summaries for compliance.

**Solution:** PDF/CSV report generator with templates for:
- Daily trade blotter (all orders with timestamps, fills, commissions)
- Weekly risk summary (VaR, CVaR, drawdown, position concentrations)
- Monthly performance attribution (by strategy, regime, asset class)
- Audit trail export (full immutable log for regulatory requests)

**Estimated effort:** 3-5 days

---

### 4B. Broker Abstraction Layer

**Problem:** Everything is Alpaca-specific. Adding Interactive Brokers, TD Ameritrade, or any other broker requires rewriting the entire execution layer.

**Solution:** Define a `BrokerInterface` protocol and implement Alpaca as the first adapter.

```python
# regime_platform/brokers/base.py
class BrokerInterface(Protocol):
    async def submit_order(self, order: OrderRequest) -> OrderResult: ...
    async def cancel_order(self, order_id: str) -> None: ...
    async def get_positions(self) -> list[Position]: ...
    async def get_account(self) -> AccountInfo: ...
    async def get_order_status(self, order_id: str) -> OrderStatus: ...
    async def cancel_all_orders(self) -> list[str]: ...
    async def close_all_positions(self) -> list[str]: ...

# regime_platform/brokers/alpaca.py
class AlpacaBroker(BrokerInterface): ...  # Wraps existing AlpacaExecutor

# regime_platform/brokers/ibkr.py
class IBKRBroker(BrokerInterface): ...    # Future: Interactive Brokers
```

**Frontend:** Settings page gets a "Broker Configuration" section where users select their broker and enter credentials.

**Estimated effort:** 3-5 days

---

### 4C. Historical Portfolio Equity Curve

**Problem:** No way to see "how am I doing over time?" The portfolio page shows current positions but no historical performance chart.

**Solution:** Store daily portfolio snapshots and render an equity curve.

```sql
CREATE TABLE portfolio_snapshots (
    id           BIGSERIAL PRIMARY KEY,
    user_id      VARCHAR(255) NOT NULL,
    date         DATE NOT NULL,
    total_equity DECIMAL(18, 2),
    cash         DECIMAL(18, 2),
    positions_value DECIMAL(18, 2),
    unrealized_pnl DECIMAL(18, 2),
    realized_pnl   DECIMAL(18, 2),
    daily_return   DECIMAL(8, 6),
    num_positions  INTEGER,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, date)
);
```

**Frontend:** Area chart showing equity curve with regime-colored background bands (so PMs can see which regimes produced gains/losses).

**Estimated effort:** 2-3 days

---

### 4D. Notification Preferences

**Problem:** No way to configure alert thresholds or delivery channels. Discord webhooks are hardcoded.

**Solution:** User preferences stored in Clerk metadata with UI for:
- Alert thresholds (e.g., "notify when P&L drops > 2%")
- Delivery channels (Discord, Telegram, Email, In-app)
- Quiet hours
- Per-symbol watchlists with custom thresholds

**Estimated effort:** 2-3 days

---

## 5. Implementation Roadmap

```
Week 1-2: P0 — Live Trading Blockers
├── Day 1-2:   Kill Switch (backend service + middleware)
├── Day 3-4:   Kill Switch (frontend panel + BFF routes)
├── Day 5-7:   Persistent Audit Log (DB schema + backend service)
├── Day 8-9:   Audit Log (frontend viewer + export)
├── Day 10-11: Paper/Live Toggle (backend mode manager + confirmation flow)
├── Day 12-13: Fill Reconciliation (backend service + frontend panel)
└── Day 14:    Integration testing + end-to-end walkthrough

Week 3-4: P1 — Production Hardening
├── Day 15-17: Multi-Tenant Isolation (replace userId: "default" everywhere)
├── Day 18:    Alpaca Rate Throttle
├── Day 19-20: Server-Side Auth Middleware
├── Day 21-23: Real-Time P&L Dashboard
└── Day 24:    Per-Feature RBAC

Week 5-6: P2 — Institution-Grade (optional, can be spread over time)
├── Day 25-29: Compliance Reporting Module
├── Day 30-34: Broker Abstraction Layer
├── Day 35-37: Historical Equity Curve
└── Day 38-40: Notification Preferences
```

---

## 6. New Files Summary

### Backend (FastAPI)

| File | Purpose | Priority |
|------|---------|----------|
| `services/operational/__init__.py` | Module init | P0 |
| `services/operational/kill_switch.py` | Three-tier kill switch service | P0 |
| `services/operational/audit_logger.py` | Append-only audit logger | P0 |
| `services/operational/mode_manager.py` | Paper/Live mode manager | P0 |
| `services/operational/reconciler.py` | Fill reconciliation service | P0 |
| `middleware/kill_switch.py` | Kill switch middleware for FastAPI | P0 |
| `middleware/alpaca_rate_limiter.py` | Alpaca API rate limiter | P1 |
| `routers/operational.py` | API routes for all operational endpoints | P0 |
| `brokers/base.py` | Broker interface protocol | P2 |
| `brokers/alpaca.py` | Alpaca adapter (wraps existing executor) | P2 |

### Frontend (Next.js)

| File | Purpose | Priority |
|------|---------|----------|
| `src/components/operational/KillSwitchPanel.jsx` | Emergency controls UI | P0 |
| `src/components/operational/AuditLogViewer.jsx` | Audit log search & export | P0 |
| `src/components/operational/ModeToggle.jsx` | Paper/Live toggle + confirmation | P0 |
| `src/components/operational/ReconciliationPanel.jsx` | Fill reconciliation status | P0 |
| `src/components/operational/LivePLDashboard.jsx` | Real-time P&L streaming | P1 |
| `src/components/shared/RoleGate.jsx` | RBAC wrapper component | P1 |
| `src/hooks/useAuth.js` | Auth hook with userId extraction | P1 |
| `src/hooks/useRole.js` | Role-based access hook | P1 |
| `src/app/api/trading/kill-switch/[action]/route.ts` | Kill switch BFF routes | P0 |
| `src/app/api/trading/mode/[action]/route.ts` | Mode toggle BFF routes | P0 |
| `src/app/api/trading/audit-log/[action]/route.ts` | Audit log BFF routes | P0 |
| `src/app/api/trading/reconcile/[action]/route.ts` | Reconciliation BFF routes | P0 |

### Database (Supabase)

| Table | Purpose | Priority |
|-------|---------|----------|
| `trade_audit_log` | Append-only trade audit trail | P0 |
| `kill_switch_state` | Active halt state (Redis-backed, DB backup) | P0 |
| `mode_state` | Paper/Live mode state | P0 |
| `portfolio_snapshots` | Daily portfolio snapshots for equity curve | P2 |

---

## 7. Risk Matrix: What Could Go Wrong in Live Trading

| Risk | Current Mitigation | Phase 8 Mitigation | Residual Risk |
|------|--------------------|---------------------|---------------|
| Runaway algo places unlimited orders | Circuit breaker (per-API-call only) | Kill switch + rate limiter + signal cooldown | Low — triple-layered |
| Accidental live mode switch | None (hardcoded paper) | Confirmation gate + acknowledgment + audit log | Very Low |
| Data feed error triggers bad signals | Regime gate + velocity filter | Kill switch auto-trigger on data_feed_error | Low |
| Fill at unexpected price | No monitoring | Reconciliation with slippage thresholds + alerts | Low |
| User sees another user's trades | None (shared userId) | Multi-tenant isolation + RLS | Very Low |
| Unauthorized order execution | Client-side auth only | Server-side middleware + RBAC | Very Low |
| No record of what happened | In-memory deque (lost on restart) | Append-only PostgreSQL audit log | Very Low |
| Can't stop trading in emergency | None | Three-tier kill switch + cancel-all + close-all | Very Low |

---

## 8. Success Criteria

Phase 8 is complete when:

- [ ] A PM can activate a global halt that prevents ALL order submissions within 1 second
- [ ] Every order submission, fill, approval, and mode change is recorded in an immutable audit log
- [ ] A PM can switch from paper to live mode with a multi-gate confirmation flow, and every mode change is audited
- [ ] Fill reconciliation runs automatically and alerts on slippage > 200 bps
- [ ] Each user sees only their own data (no cross-user data leakage)
- [ ] The Alpaca rate limiter prevents more than 30 order submissions per minute
- [ ] Server-side auth middleware blocks unauthorized access to admin/trading routes
- [ ] Real-time P&L dashboard updates within 5 seconds of a position change
- [ ] The navbar shows a persistent, pulsing `🔴 LIVE` indicator when in live mode
- [ ] A viewer-role user cannot see or click any order execution buttons

---

## 9. The Promotion Angle

Phase 8 is the story that turns heads:

> **"We built a quant engine that rivals institutional desks. Then we asked: what would it take to trust this with real money? Phase 8 is the answer — kill switches, immutable audit trails, fill reconciliation, and the operational scaffolding that separates research from production. This isn't a backtesting toy anymore. It's a live-tradable system with institutional-grade quantitative rigor AND institutional-grade operational safety."**

The institutional gap scores tell the story:

| Dimension | Before Phase 8 | After Phase 8 |
|-----------|----------------|---------------|
| Operational Readiness | 1.0 | 4.0 |
| Audit & Compliance | 1.0 | 4.0 |
| Risk Controls | 3.0 | 4.5 |
| Multi-Tenancy | 1.0 | 4.0 |
| **OVERALL** | **2.8** | **3.9** |

---

*Document version: 1.0 — Scoped for implementation*  
*Last updated: 2026-05-20*
