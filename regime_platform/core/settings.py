"""
Centralized Pydantic Settings Configuration
════════════════════════════════════════════

Replaces all scattered os.getenv() calls with a single, validated,
type-safe Settings class powered by pydantic-settings.

Usage:
    from regime_platform.core.settings import settings

    key = settings.API_KEY

Environment variables are loaded from a .env file (if present) and
can be overridden by actual environment variables at runtime.
"""

from __future__ import annotations

import logging
import os

from pydantic_settings import BaseSettings

log = logging.getLogger("regime.settings")


class Settings(BaseSettings):
    """
    Centralized application settings.

    All values can be overridden via environment variables or a .env file.
    Grouped into logical sections for clarity.
    """

    model_config = {
        "env_file": [".env", ".env.local"],
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # ── Server ──────────────────────────────────────────────────────────────
    PORT: int = 8000

    # ── Auth ────────────────────────────────────────────────────────────────
    # 2026-08-25: single shared-secret gatekeeping (see
    # regime_platform/auth/api_key.py). No live users, single Render
    # fallback deployment — JWT/Clerk/per-user roles removed. Empty means
    # the gate fails closed: no request will ever authenticate.
    API_KEY: str = ""

    # ── Noble Trader Multi-Tenant Architecture ─────────────────────────────
    # P5 (2026-07-20): noble-trader-fastapi-backend is the SWEEP ORCHESTRATOR.
    # It computes signals and delivers qualified ones directly to Discord.
    # It is NOT a trade executor anymore — execution lives in the hermes
    # agent (noble-trader-agent).
    # The flags below enforce that contract and wire in multi-tenant routing.
    #
    # NOBLE_TRADER_DISABLE_EXECUTION (default True):
    #   When True, all execution codepaths (renko/executor.py, operational/*,
    #   /live/toggle, /reconcile/*, /executor/status, /sse/pnl) raise
    #   ExecutionDisabledError. Set to False only for emergency rollback.
    NOBLE_TRADER_DISABLE_EXECUTION: bool = True

    # NOBLE_TRADER_DISABLE_L2_FEED (default True, audit HIGH #11, 2026-07-23):
    #   When True, the L2FeedManager is NOT started in main.py lifespan.
    #   The L2 imbalance value it computes (regime_platform/services/
    #   statistics/l2_imbalance.py) is NOT consumed by any live path —
    #   the v5 EV engine removed external_signal (compute_ev() no longer
    #   accepts it), and the replacement microstructure source is the
    #   proxy's SSE stream (HIGH #9 wired the agent-side consumer).
    #   Keeping the L2 WebSocket adapters running wastes one WS
    #   connection per symbol + CPU on snapshot processing.
    #
    #   Set to False ONLY for research/simulation harnesses that want
    #   to push L2 snapshots to a downstream consumer (e.g. a future
    #   `/api/ta/{symbol}/indicators?source=l2` endpoint). The
    #   L2FeedManager code is preserved (not deleted) for that path.
    NOBLE_TRADER_DISABLE_L2_FEED: bool = True

    # Plan UUIDs — resolved at startup from nobletradingapp's `plans` table.
    # Used by Discord plan-routing to bucket signals.
    # If left empty, plan routing is skipped (signals go to both streams).
    NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID: str = ""
    NOBLE_TRADER_PLAN_PRECISION_PRO_ID: str = ""

    # ── Alpaca (DATA FEED ONLY — execution deprecated) ─────────────────────
    # ALPACA_API_KEY/SECRET are still used as READ-ONLY data credentials by
    # sweep_orchestrator/price_fetcher.py and adapters/l2_adapters.py.
    # They MUST NOT be used for order placement — that path is gated behind
    # NOBLE_TRADER_DISABLE_EXECUTION above.
    ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", os.getenv("ALPACA_APIKEY", ""))
    ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", os.getenv("ALPACA_SECRETKEY", ""))
    ALPACA_BASE_URL: str = "https://paper-api.alpaca.markets"
    ALPACA_LIVE_BASE_URL: str = "https://api.alpaca.markets"
    ALPACA_PAPER_BASE_URL: str = "https://paper-api.alpaca.markets"
    ALPACA_RATE_LIMIT: int = 200        # Max Alpaca API requests per period
    ALPACA_RATE_PERIOD: int = 60         # Rate limit period in seconds

    # ── Binance ─────────────────────────────────────────────────────────────
    BINANCE_TESTNET: bool = False

    # ── Hyperliquid ──────────────────────────────────────────────────────────
    HL_API_URL: str = "https://api.hyperliquid.xyz"
    HL_WS_URL: str = "wss://api.hyperliquid.xyz/ws"

    # ── TradingView (RapidAPI) ──────────────────────────────────────────────
    # TradingView API via RapidAPI provides OHLCV candle data for crypto,
    # forex, stocks, commodities, and more. Requires a RapidAPI key.
    # Sign up at https://rapidapi.com/ and subscribe to the TradingView API.
    # Plan: Ultra (100k requests/month)
    TRADINGVIEW_API_KEY: str = os.getenv("TRADINGVIEW_API_KEY", "")
    TRADINGVIEW_API_HOST: str = os.getenv("TRADINGVIEW_API_HOST", "tradingview-data1.p.rapidapi.com")
    TRADINGVIEW_BASE_URL: str = os.getenv("TRADINGVIEW_BASE_URL", "https://tradingview-data1.p.rapidapi.com")

    # ── Interactive Brokers ─────────────────────────────────────────────────
    IB_HOST: str = "127.0.0.1"
    IB_PORT: int = 7497
    IB_CLIENT_ID: int = 1

    # ── Discord ─────────────────────────────────────────────────────────────
    DISCORD_WEBHOOK_SIGNALS: str = ""
    DISCORD_WEBHOOK_EXECUTIONS: str = ""
    DISCORD_WEBHOOK_STATUS: str = ""
    # ── Per-asset-class signal webhooks (optional) ───────────────────────
    # If set, trade signals are routed to these channels by asset class
    # instead of all going to DISCORD_WEBHOOK_SIGNALS. This splits signals
    # across channels so traders only see signals for their asset class:
    #   crypto   → #crypto-signals    (BTC, ETH, SOL, etc.)
    #   equities → #equities-signals  (SPY, QQQ, etc.)
    #   metals   → #metals-signals    (Gold, Silver, etc.)
    # Leave empty to use the default DISCORD_WEBHOOK_SIGNALS for all.
    DISCORD_WEBHOOK_CRYPTO: str = ""
    DISCORD_WEBHOOK_EQUITIES: str = ""
    DISCORD_WEBHOOK_METALS: str = ""
    DISCORD_WEBHOOK_FOREX: str = ""
    # ── Per-plan signal webhooks (P5 — multi-tenant routing) ─────────────
    # When set, signals are routed to the plan-specific Discord channel
    # based on nt_symbol.plan_ids membership. This is the canonical
    # 4-channel Discord layout for Noble Trader's multi-tenant model:
    #   1. #signal-scout       ← Signal Scout ($79/mo) subscriber signals
    #   2. #precision-pro      ← Precision Pro ($199/mo) subscriber signals
    #   3. #system-status      ← System health + regime updates (DISCORD_WEBHOOK_STATUS)
    #   4. #trade-executions   ← Hermes agent execution alerts (DISCORD_WEBHOOK_EXECUTIONS)
    # If both plan webhooks are empty, signals fall back to per-asset-class
    # routing, then to DISCORD_WEBHOOK_SIGNALS.
    DISCORD_WEBHOOK_SIGNAL_SCOUT: str = ""
    DISCORD_WEBHOOK_SIGNAL_PRECISION: str = ""

    # ── Telegram ────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # ── Rate Limiting ───────────────────────────────────────────────────────
    RATE_LIMIT_DEFAULT: str = "60/minute"
    RATE_LIMIT_COMPUTE: str = "10/minute"
    RATE_LIMIT_WRITE: str = "20/minute"

    # ── Supabase ─────────────────────────────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""

    # ── Supabase Realtime broadcast (Talaria dashboard) ─────────────────────
    # NOBLE_REALTIME_BROADCAST=1 (default) enables fire-and-forget broadcast
    # publishing of qualified signals (realtime:signals) and paper portfolio
    # events (realtime:paper). Set to "0" to disable publishing entirely.
    NOBLE_REALTIME_BROADCAST: str = "1"

    # NOBLE_TRADER_DISCORD_SIGNALS_ENABLED (default True):
    #   When True, qualified buy/sell signals are sent DIRECTLY to the
    #   Discord notifier from BOTH sweep paths (light _process_symbol +
    #   heavy _push_heavy_signal) — the only Discord delivery path since
    #   Redis (and the SignalAggregator that used to relay to Discord from
    #   it) was fully retired. Set "0" to disable direct Discord delivery
    #   entirely. (2026-08-10 — incident fix, Option 2)
    NOBLE_TRADER_DISCORD_SIGNALS_ENABLED: bool = True

    # EOD_CALIBRATION_ENFORCE (default True):
    #   When True, the sweep ENFORCES the EOD calibration-bias correction at
    #   qualification time: symbols whose |bias| > 0.10 (OVERCONFIDENT /
    #   UNDERCONFIDENT, per the EOD classification) have their blended p_win
    #   shrunk toward 0.5 (Bayesian penalty) and their effective Kelly
    #   halved before the qualification gates run. This is the consumer side
    #   of the EOD self-correction loop (the EOD pass writes nt_eod_actions
    #   recommendations; the sweep applies them live). Set "0" to disable
    #   enforcement (recommendations still logged).
    EOD_CALIBRATION_ENFORCE: bool = True

    # EOD_RESOLVE_LIMIT (default 1000, 2026-08-12):
    #   Nightly cap for how many pending nt_signal_sim rows the EOD resolver
    #   forward-resolves at the 00:30 UTC lookback. Was hardcoded 200 — the
    #   live capture rate is ~3,200 signals/day, so 200/night let the pending
    #   backlog grow by ~3,000/day (17,992 rows / 16,792 pending observed
    #   2026-08-12). Raise for faster drain; the one-time backlog drain is
    #   scripts/drain_signal_backlog.py.
    EOD_RESOLVE_LIMIT: int = 1000

    # EOD_RESOLVE_MAX_TOTAL (default 12000, 2026-08-13):
    #   Cap on TOTAL rows the EOD resolver may attempt per nightly run
    #   (H4 backlog fix). The resolver batch-loops until the in-window
    #   pending backlog is drained or this cap is hit. Raised from 4000
    #   on 2026-08-13 after the backlog audit: capture ≈ 3,200–4,000/day
    #   and a 4,000 cap made the nightly run a treadmill (pending stuck
    #   at ~18k, oldest pending aging toward the 7-day retention expiry).
    #   12000 ≈ 3 days of capture per run → the ledger drains ~8k/night
    #   and the backlog clears without manual intervention.
    EOD_RESOLVE_MAX_TOTAL: int = 12000

    # EOD_RETENTION_RESOLVED_DAYS (default 30, 2026-08-12):
    #   Retention pass (eod_retention.run_eod_retention, wired into the
    #   nightly _run_eod_lookback Part IV) DELETEs resolved nt_signal_sim
    #   rows (outcome IS NOT NULL) older than this many days. Keeps the
    #   signal ledger bounded; calibration records live separately in
    #   nt_calibration_records so no bias data is lost.
    EOD_RETENTION_RESOLVED_DAYS: int = 30

    # EOD_RETENTION_STALE_DAYS (default 7, 2026-08-12):
    #   Retention pass marks unresolved nt_signal_sim rows (outcome IS NULL)
    #   older than this many days as 'expired' via PATCH — they have sat past
    #   the 48-bar expiry window with no touch and are neither wins nor
    #   losses, so no calibration record is written (same convention as the
    #   resolver's close-walk expiry). Frees the pending index and keeps the
    #   resolver from re-picking stale rows forever.
    EOD_RETENTION_STALE_DAYS: int = 7

    # ── Intraday recalibration cadence (2026-08-13 plan, user-approved) ─────
    # Bias is recomputed + nt_eod_actions rewritten every EOD_CORRECTION_INTERVAL_HOURS
    # (4h) via correction-only passes at 04:30/08:30/12:30/16:30/20:30 UTC in
    # addition to the nightly 00:30 full lookback. A 24h-only cadence left
    # un-flagged symbols trading on stale p_win for up to a day after a regime
    # shift. Master toggle: EOD_CORRECTION_INTRADAY_ENABLED.

    EOD_CORRECTION_INTRADAY_ENABLED: bool = True
    EOD_CORRECTION_INTERVAL_HOURS: int = 4

    # How old an EOD action may be before enforcement stops applying it.
    # Shrunk 26 → 8h to match the 4h correction cadence (actions are refreshed
    # every 4h, so an 8h window always has a fresh penalty while stale ones age
    # out fast). Mirrored by calibration_enforcement.ACTION_FRESH_WINDOW_HOURS
    # via a fallback constant (a test asserts they stay in sync).
    EOD_ACTION_FRESH_HOURS: int = 8

    # Rolling bias window (C): fetch_calibration_bias computes bias over records
    # with entered_at within the last EOD_BIAS_WINDOW_DAYS days (min
    # EOD_BIAS_MIN_RECORDS) instead of all-time, so a regime shift moves the
    # bias in hours, not days.
    EOD_BIAS_WINDOW_DAYS: int = 7
    EOD_BIAS_MIN_RECORDS: int = 20

    # EOD_BIAS_MIN_DISTINCT_DAYS retired 2026-09-08 (worklog
    # 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md): the distinct-
    # calendar-days clustering discount it drove could be evaded by a
    # burst that happened to span exactly this many calendar dates while
    # still being one continuous overlapping episode (USDCHF: 294 records,
    # 1 real episode, 3 calendar dates == this setting's own default).
    # Replaced by real episode counting (see
    # fetch_calibration_bias/_group_into_episodes, supabase_client.py) —
    # min-episode threshold now lives in config/feature_flags.yaml's
    # calibration_bias_min_episodes (regime_platform/core/feature_flags.py's
    # get_calibration_bias_min_episodes()), not here, per this session's
    # config-centralization discipline.

    # EOD_BIAS_DECAY_HALFLIFE_DAYS (default 3, 2026-08-25):
    #   Records within EOD_BIAS_WINDOW_DAYS are weighted by recency
    #   (weight = 0.5 ** (age_days / this)) instead of counting equally up to
    #   an abrupt cutoff — smooths the "bias swings sharply the moment a bad
    #   week rolls out of the 7-day window" failure mode without widening the
    #   window itself (which would slow reaction to genuine regime shifts).
    EOD_BIAS_DECAY_HALFLIFE_DAYS: float = 3.0

    # This is one of THREE calibration-trust thresholds in the platform
    # (this one, EOD_BIAS_ENFORCED_THRESHOLD below, and
    # drift_monitor.py's EOD_DRIFT_THRESHOLD) — see eod_correction.py's
    # module-level comment above OVERCONFIDENT_BIAS for the full tiering
    # table (2026-09-09, worklog
    # 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16).
    #
    # EOD_BIAS_RAW_OVERCONFIDENT (default 0.30, 2026-08-22):
    #   Threshold for the 4h intraday correction pass (raw-bias mode). The raw
    #   p_win (predicted_p_win_raw, pre-enforcement) is naturally more extreme
    #   than the enforced value — raw model output ranges 0.0–1.0 while enforced
    #   values are shrunk toward 0.5 by the Bayesian penalty. This wider band
    #   prevents over-triggering on normal model variance. The user's target
    #   range is -0.2 < bias < 0.7; this is the overconfidence boundary within
    #   that range (bias >= 0.30 → OVERCONFIDENT in raw mode).
    EOD_BIAS_RAW_OVERCONFIDENT: float = 0.30

    # EOD_BIAS_RAW_UNDERCONFIDENT (default -0.20, 2026-08-22):
    #   Underconfidence threshold for raw-bias mode (4h correction pass only).
    #   Mirrors EOD_BIAS_RAW_OVERCONFIDENT but for the negative side. Symbols
    #   with raw bias <= -0.20 are flagged UNDERCONFIDENT.
    EOD_BIAS_RAW_UNDERCONFIDENT: float = -0.20

    # EOD_BIAS_ENFORCED_THRESHOLD (default 0.10, 2026-08-22 audit fix #2):
    #   Centralized value for the nightly/enforced-bias band (|bias| > this
    #   -> OVERCONFIDENT/UNDERCONFIDENT). MUST mirror
    #   eod_correction.OVERCONFIDENT_BIAS / eod_correction.UNDERCONFIDENT_BIAS
    #   (a test asserts they stay in sync — see
    #   worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §4 fix #2).
    #   The public /calibration/bias and /calibration/status endpoints read
    #   this setting directly so they can never silently drift from the
    #   threshold the live enforcement loop actually acts on.
    EOD_BIAS_ENFORCED_THRESHOLD: float = 0.10

    # MARKOV_EMA_ALPHA (default 0.30, 2026-08-22 audit fix #3a):
    #   EMA smoothing factor for the per-symbol P_markov_hold_N estimate:
    #   smoothed = alpha * raw + (1 - alpha) * previous_smoothed.
    #   Root cause (AAPL/US500, worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md
    #   §2 Mechanism 1): in calm/low-volatility markets, the HMM's discretized
    #   current_state (UP/DOWN/FLAT) can flip tick-to-tick at the
    #   classification boundary, and each state's transition-matrix row is
    #   near-deterministic — so the raw T^N hold probability swings between
    #   near-0 and near-1 every 5 minutes even though the market hasn't
    #   moved. Lower alpha = more smoothing (slower to react to genuine
    #   regime changes); higher alpha = closer to the old unsmoothed
    #   behaviour. alpha=1.0 disables smoothing entirely (== previous
    #   behaviour, useful for a quick rollback without a code change).
    MARKOV_EMA_ALPHA: float = 0.30

    # REGIME_EMA_ALPHA (default 0.30, 2026-08-23 R1 fix #3 — worklog/
    #   20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md):
    #   Same EMA-smoothing formula as MARKOV_EMA_ALPHA, applied to the HMM
    #   trend posterior (trend_probs) before it drives p_regime in the EV
    #   blend. Supplementary to the root fixes in regime_engine.py (a
    #   data-relative min_covar floor + label-continuity matching across
    #   refits) — this alone does not fix the regime HMM's instability, it
    #   only dampens residual tick-to-tick noise on top of those fixes.
    #   Does NOT affect regime_label/regime_shift bookkeeping (Layer 2),
    #   which stays driven by the raw prediction.
    REGIME_EMA_ALPHA: float = 0.30

    # REGIME_NONCONVERGENCE_WEIGHT_SCALE (default 0.5, 2026-08-23 R4 —
    #   worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md):
    #   When the trend HMM's EM fit did not converge cleanly this tick
    #   (RegimeSnapshot.trend_converged is False — see
    #   RegimeHMM._fit_converged), p_regime's blend weight is multiplied by
    #   this factor before compute_ev() runs (via EVEngine.temp_weights()),
    #   same mechanism already used for the TimesFM soft gate. 1.0 disables
    #   the discount entirely (== previous behaviour, quick rollback without
    #   a code change). A poorly-conditioned fit is exactly the AAPL/US500
    #   condition this whole investigation is about — "Model is not
    #   converging" was observed on BOTH symbols' real feature data — so an
    #   unreliable regime read contributes less to that tick's p_win instead
    #   of being trusted at full weight regardless of fit quality.
    REGIME_NONCONVERGENCE_WEIGHT_SCALE: float = 0.5

    # KELLY_REGIME_FLOOR_REQUIRE_NONNEG_MU (default True, 2026-08-22 audit fix #3b):
    #   The regime-based Kelly floor (orchestrator.py, "Kelly was 0 — applied
    #   regime floor") exists so sampling noise on hourly returns doesn't zero
    #   out sizing in a genuinely-edged regime. But it previously fired
    #   whenever kelly_f==0.0 regardless of WHY — including when the measured
    #   mean historical return (mu) was actually negative (confirmed live for
    #   AAPL: mu=-0.00007129, i.e. a real negative edge, still got force-sized
    #   to kelly_f=0.0397 purely from high regime confidence). When True, the
    #   floor only fires if mu >= 0 — a negative measured edge is left at
    #   kelly_f=0.0 (no size) instead of being overridden. Set False to
    #   restore the original always-floor behaviour.
    KELLY_REGIME_FLOOR_REQUIRE_NONNEG_MU: bool = True

    # ── Stagnation breaker (2026-08-22 audit fix #3c) ──────────────────────
    # Live evidence: AAPL/US500 stayed OVERCONFIDENT at BIT-FOR-BIT IDENTICAL
    # raw bias across 6+ consecutive 4h correction passes (18+ hours) —
    # emergency_heavy_sweep/bayesian_penalty/kelly_reduction re-fired every
    # cycle without resolving, because neither mechanism (HMM state-flip
    # noise, negative-mu Kelly floor) is something a parameter re-sweep can
    # fix. This is a GENERALIZED detector: it doesn't diagnose why a symbol
    # is stuck, only THAT it is (bias unmoved by more than
    # STAGNATION_EPSILON across consecutive raw-bias correction cycles), and
    # escalates automatically — no human gate, per user direction.
    #   Tier 0 (streak < TIER1_CYCLES):  normal actions, unchanged.
    #   Tier 1 (streak >= TIER1_CYCLES): deeper penalty — kelly reduction
    #     factor drops from 0.5 to STAGNATION_KELLY_REDUCTION_TIER1.
    #   Tier 2 (streak >= TIER2_CYCLES): hard kill switch — kelly_f forced
    #     to 0.0 (blocks publishing entirely) via a "stagnation_kill_switch"
    #     action.
    #   Auto-recovery: (a) immediate — the unconditional stale-action-clear
    #     at each 4h cycle boundary already lifts everything the moment the
    #     symbol's raw bias returns to the calibrated band; (b) time-boxed —
    #     once the streak reaches STAGNATION_RESET_CYCLES, the tier ladder
    #     resets to 0 and a fresh emergency_heavy_sweep is forced, so a
    #     truly-stuck symbol gets periodic new attempts rather than a
    #     permanent, unrecoverable block.
    STAGNATION_EPSILON: float = 0.02
    STAGNATION_TIER1_CYCLES: int = 2
    STAGNATION_TIER2_CYCLES: int = 4
    STAGNATION_RESET_CYCLES: int = 8
    STAGNATION_KELLY_REDUCTION_TIER1: float = 0.25

    # TUNING_AUTONOMY_ENABLED (default True, 2026-08-23, Layer 0 of the
    # tuning registry): global kill switch for ALL autonomous/agent-driven
    # writes to regime_platform.core.tuning_registry (Layer 1's continuous
    # tuner, Layer 2's agent-triggered adjustments). This is deliberately
    # the ONE human-facing control in the whole registry design — not a
    # per-change review gate (that doesn't fire in practice, see
    # worklog/PARAMETER_TUNING_PLAYBOOK.md §1 revision note), just a single
    # switch to freeze all autonomous tuning at once if ever needed. Set
    # "0" in Studio Secrets to disable; reads are unaffected (registry
    # reads still work, only writes are blocked).
    TUNING_AUTONOMY_ENABLED: bool = True

    # REGIME_SHIFT_ALERT_THRESHOLD (default 0.35, 2026-08-23, Layer 2 —
    # worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.3): minimum shift
    # magnitude (EVEngine.detect_regime_shift()'s total-variation
    # distance between consecutive regime posteriors, 0-1) that triggers
    # a `regime_shift_alert` row in nt_eod_actions. This is a DETECTION
    # threshold, not a Tier D auto-tuned parameter — it decides how
    # sensitive the Layer 2 trigger is, which is a judgment call best
    # left to a human/operator rather than the autonomous Tier D loop.
    REGIME_SHIFT_ALERT_THRESHOLD: float = 0.35

    # Drift monitor (D): on each light sweep, per symbol, a rolling
    # EOD_DRIFT_WINDOW_HOURS realized win rate is compared against the mean
    # predicted p_win. If the deviation exceeds EOD_DRIFT_THRESHOLD with at
    # least EOD_DRIFT_MIN_SAMPLE records AND no fresh action exists, an
    # immediate penalty action is written (bypasses the 4h cadence).
    EOD_DRIFT_MONITOR_ENABLED: bool = True
    EOD_DRIFT_WINDOW_HOURS: int = 48
    EOD_DRIFT_MIN_SAMPLE: int = 20
    EOD_DRIFT_THRESHOLD: float = 0.15

    # BRACKET_REACHABILITY_ENABLED (default True, 2026-08-25, Phase 1 of
    # worklog/20260825_CALIBRATION_REFRAME_SCOPE.md): kill switch for the
    # weekly bracket-reachability diagnostic job (bracket_reachability.py).
    # Read-only — computes and upserts nt_bracket_reachability from
    # resolved nt_signal_sim rows; nothing downstream reads it yet.
    BRACKET_REACHABILITY_ENABLED: bool = True

    # CALIBRATION_BIAS_BY_BUCKET_ENABLED (default True, 2026-08-25, Phase 2
    # of worklog/20260825_CALIBRATION_REFRAME_SCOPE.md): kill switch for
    # the calibration-bias-by-bucket + entry-drift diagnostics
    # (calibration_bias_bucket.py), which run right after the weekly
    # bracket-reachability job. Read-only — upserts
    # nt_calibration_bias_by_bucket / nt_entry_drift; nothing downstream
    # reads either yet.
    CALIBRATION_BIAS_BY_BUCKET_ENABLED: bool = True

    # MACRO_REGIME_ENABLED (default True, 2026-08-28, worklog/
    # 20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md "Macro filter — full
    # build scope"): kill switch for BOTH the daily macro-regime refresh
    # (macro_filter.py, 06:00 UTC) and the weekly win/loss+PnL evidence
    # diagnostic (macro_edge.py, chained onto the existing bracket-
    # reachability weekly job). Live-behavior gating for the filter
    # itself is the separate macro_regime_mode in
    # config/feature_flags.yaml (off|shadow|live) — this flag only
    # controls whether the two background jobs run at all.
    MACRO_REGIME_ENABLED: bool = True

    # HMM_SHADOW_ACCURACY_ENABLED (default True, 2026-08-31, worklog/
    # 20260831_SHADOW_TO_LIVE_ROADMAP.md): kill switch for the weekly
    # outcome-linked accuracy diagnostic (hmm_shadow_accuracy.py),
    # chained onto the existing bracket-reachability weekly job. Informs
    # the Tier 1/2 (markov_source_mode/trend_probs_gate_mode) live-mode
    # decision review — does not itself change either mode.
    HMM_SHADOW_ACCURACY_ENABLED: bool = True

    # Emergency heavy-sweep cooldown for the intraday correction passes:
    # a symbol gets at most one re-optimization per this window (bounds
    # backtest cost; the nightly pass is unaffected).
    EOD_HEAVY_SWEEP_COOLDOWN_HOURS: int = 12

    # predicted_p_win_raw capture (2026-08-13 plan, SCHEDULED): when True, the
    # PRE-enforcement p_win is persisted into nt_signal_sim (new column, see
    # supabase/migrations/114_add_predicted_p_win_raw.sql) and carried into
    # nt_calibration_records so the calibration loop can separate model
    # miscalibration from enforcement compensation. DEFAULT OFF — the DB column
    # must exist BEFORE enabling (PostgREST would otherwise reject the insert
    # with PGRST204 and signals would stop being captured).
    NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW: bool = False

    # NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY (default False, 2026-08-12):
    #   When True, only QUALIFIED non-neutral signals are persisted to
    #   nt_signal_sim (both sweep paths). The ledger's purpose is measuring
    #   prediction-vs-outcome bias, so capturing every non-neutral signal
    #   (qualified or not) is the default; enabling this trims ~93% of the
    #   noise rows but changes what the sim measures — opt-in by design.
    NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY: bool = False

    # ── Computed properties ─────────────────────────────────────────────────

    @property
    def discord_configured(self) -> bool:
        """True if any Discord webhook URL is configured."""
        return bool(
            self.DISCORD_WEBHOOK_SIGNALS
            or self.DISCORD_WEBHOOK_EXECUTIONS
            or self.DISCORD_WEBHOOK_STATUS
        )

    @property
    def telegram_configured(self) -> bool:
        """True if Telegram bot token and chat ID are configured."""
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID)

    # ── Validation ──────────────────────────────────────────────────────────

    def model_post_init(self, __context: object) -> None:
        """Validate configuration and warn about missing settings."""
        # Authentication warning
        if not self.API_KEY:
            log.warning(
                "API_KEY is not set. All authenticated endpoints will reject "
                "every request (the gate fails closed by design)."
            )

        # Telegram configuration warning
        if self.TELEGRAM_BOT_TOKEN and not self.TELEGRAM_CHAT_ID:
            log.warning("Telegram bot token configured but no chat ID. Telegram signals will be disabled.")
        elif self.TELEGRAM_CHAT_ID and not self.TELEGRAM_BOT_TOKEN:
            log.warning("Telegram chat ID configured but no bot token. Telegram signals will be disabled.")


# ── Singleton ────────────────────────────────────────────────────────────────

settings = Settings()
