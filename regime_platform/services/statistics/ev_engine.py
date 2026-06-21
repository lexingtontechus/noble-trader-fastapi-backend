	
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

# Note: Database connection is handled by the caller (passing a session) 
# or using the global engine if available. To keep it decoupled, 
# we define a helper that takes the session.

# ── EV Engine v3 defaults ────────────────────────────────────────────
# 4-source blend when TimesFM is available
P_WIN_WEIGHTS_TIMESFM = {
    "p_regime": 0.35,
    "p_imbalance": 0.20,
    "p_markov": 0.20,
    "p_timesfm": 0.25,
}

# 3-source fallback when TimesFM is unavailable
P_WIN_WEIGHTS_FALLBACK = {
    "p_regime": 0.40,
    "p_imbalance": 0.30,
    "p_markov": 0.30,
}

# Regime shift meta-signal thresholds
TIMESFM_CONFIDENCE_MIN = 0.50  # Min TimesFM confidence to override HMM
CALIBRATION_OVERCONFIDENT_THRESHOLD = 0.05


@dataclass
class EVResult:
    p_win: float
    ev: float
    rr: float
    allowed: bool
    reason: Optional[str] = None


@dataclass
class EVFullResult:
    """Full EV computation result with blended P_win breakdown and aggression dispatch.

    v3: Added ``p_timesfm`` field for the 4-source TimesFM blend.
    """
    p_win: float
    p_regime: float
    p_imbalance: float
    p_markov: float
    p_timesfm: float = 0.0          # v3: TimesFM forecast component
    ev: float = 0.0
    ev_per_dollar: float = 0.0
    rr: float = 0.0
    profit: float = 0.0
    stake: float = 0.0
    allowed: bool = False
    aggression: str = "aggressive"   # "passive" | "mid" | "aggressive"
    ev_scale: float = 0.0
    reason: Optional[str] = None


class EVEngine:
    """
    Mathematical edge validation layer.
    Computes Expected Value (EV) based on historical pattern performance
    and blended P_win from regime posteriors, L2 imbalance, Markov transitions,
    **and TimesFM foundation model forecasts (v3).**
    
    v3 Changes:
      - ``compute_ev()`` accepts optional ``p_timesfm`` for 4-source blend
      - ``validate_regime_shift()`` meta-signal gates HMM shifts with TimesFM
      - Weights configurable via ``set_weights()`` at runtime
    """
    
    def __init__(self, min_ev: float = 0.0, min_rr: float = 2.0):
        self.min_ev = min_ev
        self.min_rr = min_rr
        # Mutable weights — can be overridden at runtime
        self._timesfm_weights: Dict[str, float] = dict(P_WIN_WEIGHTS_TIMESFM)
        self._fallback_weights: Dict[str, float] = dict(P_WIN_WEIGHTS_FALLBACK)

    # ── Runtime weight override ─────────────────────────────────────

    def set_weights(self, weights: Dict[str, float]) -> None:
        """
        Override the blended P_win weights at runtime.

        Accepts a dict with any subset of keys:
        ``p_regime``, ``p_imbalance``, ``p_markov``, ``p_timesfm``.
        Missing keys keep their current values.
        """
        for key in ("p_regime", "p_imbalance", "p_markov", "p_timesfm"):
            if key in weights:
                self._timesfm_weights[key] = float(weights[key])

    def get_weights(self) -> Dict[str, float]:
        """Return the current active P_win weights."""
        return dict(self._timesfm_weights)

    # ── P_regime from HMM posteriors ────────────────────────────────

    @staticmethod
    def compute_p_regime(trend_probs: Dict[str, float], direction: str) -> float:
        """
        Compute P_regime from HMM trend posteriors.
        
        P_regime(long)  = P(bullish states) + 0.5 * P(neutral)
        P_regime(short) = P(bearish states) + 0.5 * P(neutral)
        
        Handles 2-state, 3-state, and 4-state HMM label variants:
          2: bear, bull
          3: bear, neutral, bull
          4: strong_bear, bear, bull, strong_bull
        """
        if not trend_probs:
            return 0.50
        
        bullish_keys = {"bull", "strong_bull"}
        bearish_keys = {"bear", "strong_bear"}
        neutral_keys = {"neutral"}
        
        p_bullish = sum(v for k, v in trend_probs.items() if k in bullish_keys)
        p_bearish = sum(v for k, v in trend_probs.items() if k in bearish_keys)
        p_neutral = sum(v for k, v in trend_probs.items() if k in neutral_keys)
        
        if direction == "long":
            return p_bullish + 0.5 * p_neutral
        elif direction == "short":
            return p_bearish + 0.5 * p_neutral
        else:
            return 0.50

    # ── P_imbalance from L2 order book ─────────────────────────────

    @staticmethod
    def compute_p_imbalance(imbalance_signal: Optional[Dict], direction: str) -> float:
        """
        Compute P_imbalance from L2 order book signal.
        
        If aligned with direction: confidence * (0.6 + 0.4 * persistence_bonus)
        If flat: 0.50 (neutral)
        If opposed: 1.0 - confidence
        Default when no L2 data: 0.50
        """
        if not imbalance_signal:
            return 0.50
        
        try:
            signal = imbalance_signal.get("signal", "flat")
            confidence = float(imbalance_signal.get("confidence", 0.5))
            persistence = float(imbalance_signal.get("persistence", 0.0))
            
            # Normalize confidence to [0, 1]
            confidence = max(0.0, min(1.0, confidence))
            persistence = max(0.0, min(1.0, persistence))
            
            # Determine alignment
            signal_lower = str(signal).lower()
            
            # Check if signal aligns with direction
            is_bullish_signal = signal_lower in ("buy", "long", "bullish", "bid_heavy", "bid")
            is_bearish_signal = signal_lower in ("sell", "short", "bearish", "ask_heavy", "ask")
            
            if signal_lower == "flat" or (not is_bullish_signal and not is_bearish_signal):
                return 0.50
            
            # Check alignment
            aligned = (
                (direction == "long" and is_bullish_signal) or
                (direction == "short" and is_bearish_signal)
            )
            opposed = (
                (direction == "long" and is_bearish_signal) or
                (direction == "short" and is_bullish_signal)
            )
            
            if aligned:
                persistence_bonus = min(1.0, persistence)
                return confidence * (0.6 + 0.4 * persistence_bonus)
            elif opposed:
                return 1.0 - confidence
            else:
                return 0.50
        except Exception:
            return 0.50

    # ── P_markov from Markov transition matrix ─────────────────────

    @staticmethod
    def compute_p_markov(
        transition_matrix: Optional[Dict[str, Dict[str, float]]],
        current_state: str,
        direction: str,
    ) -> float:
        """
        Compute P_markov from Markov transition matrix.
        
        P_markov = T[current_state][target_state]
        e.g. T["UP"]["UP"] for long, T["UP"]["DOWN"] for short
        Default when no Markov data: 0.50
        """
        if not transition_matrix or not current_state:
            return 0.50
        
        try:
            target_state = "UP" if direction == "long" else "DOWN"
            state_probs = transition_matrix.get(current_state, {})
            if not state_probs:
                return 0.50
            return float(state_probs.get(target_state, 1.0 / 3))
        except Exception:
            return 0.50

    # ── Aggression Dispatcher ──────────────────────────────────────

    @staticmethod
    def compute_aggression(ev_per_dollar: float) -> str:
        """Map ev_per_dollar to aggression level."""
        if ev_per_dollar > 0.40:
            return "passive"
        elif ev_per_dollar > 0.15:
            return "mid"
        else:
            return "aggressive"

    @staticmethod
    def compute_ev_scale(ev_per_dollar: float) -> float:
        """Compute EV-scaled Kelly factor."""
        if ev_per_dollar > 0:
            return min(1.0, ev_per_dollar / 0.50)
        return 0.0

    # ── Core: compute_ev (full blended P_win, v3) ─────────────────

    def compute_ev(
        self,
        trend_probs: Optional[Dict[str, float]] = None,
        direction: str = "long",
        imbalance_signal: Optional[Dict] = None,
        transition_matrix: Optional[Dict[str, Dict[str, float]]] = None,
        current_state: str = "FLAT",
        tp_bricks: int = 5,
        sl_bricks: int = 3,
        brick_size: float = 0.50,
        p_timesfm: Optional[float] = None,
    ) -> EVFullResult:
        """
        Full EV computation with blended P_win (v3).

        When ``p_timesfm`` is provided and non-None, a 4-source blend
        is used::

            P_win = w_r * P_regime + w_i * P_imbalance + w_m * P_markov + w_t * P_timesfm

        Default weights (4-source):  ``(0.35, 0.20, 0.20, 0.25)``
        Fallback (3-source, no TimesFM): ``(0.40, 0.30, 0.30)``

        Weights can be overridden at runtime via ``set_weights()``.
        """
        # Step 1: Compute individual P_win sources
        p_regime = self.compute_p_regime(trend_probs or {}, direction)
        p_imbalance = self.compute_p_imbalance(imbalance_signal, direction)
        p_markov = self.compute_p_markov(transition_matrix, current_state, direction)
        p_timesfm_actual = p_timesfm if p_timesfm is not None else 0.0

        # Step 2: Blended P_win — 4-source or 3-source fallback
        if p_timesfm is not None:
            w = self._timesfm_weights
            p_win = (
                w["p_regime"] * p_regime
                + w["p_imbalance"] * p_imbalance
                + w["p_markov"] * p_markov
                + w["p_timesfm"] * p_timesfm
            )
        else:
            w = self._fallback_weights
            p_win = (
                w["p_regime"] * p_regime
                + w["p_imbalance"] * p_imbalance
                + w["p_markov"] * p_markov
            )
        
        # Step 3: EV computation
        profit = tp_bricks * brick_size
        stake = sl_bricks * brick_size
        ev = (p_win * profit) - ((1.0 - p_win) * stake)
        ev_per_dollar = ev / stake if stake > 0 else 0.0
        rr = profit / stake if stake > 0 else 0.0
        
        # Step 4: Aggression dispatch
        aggression = self.compute_aggression(ev_per_dollar)
        ev_scale = self.compute_ev_scale(ev_per_dollar)
        
        # Step 5: Allow/deny gate
        allowed = (ev > self.min_ev) and (rr >= self.min_rr)
        reason = None
        if not allowed:
            reason = (
                f"EV=${ev:.2f}, RR={rr:.2f}, ev/$={ev_per_dollar:.3f} "
                f"(Min EV={self.min_ev}, Min RR={self.min_rr})"
            )
        
        return EVFullResult(
            p_win=round(p_win, 4),
            p_regime=round(p_regime, 4),
            p_imbalance=round(p_imbalance, 4),
            p_markov=round(p_markov, 4),
            p_timesfm=round(p_timesfm_actual, 4),
            ev=round(ev, 4),
            ev_per_dollar=round(ev_per_dollar, 4),
            rr=round(rr, 4),
            profit=round(profit, 4),
            stake=round(stake, 4),
            allowed=allowed,
            aggression=aggression,
            ev_scale=round(ev_scale, 4),
            reason=reason,
        )

    # ── Existing: validate_signal (async, DB lookup) ──────────────

    async def validate_signal(self, db_session, regime: str, pattern: str, 
                                expected_profit: float, expected_loss: float) -> EVResult:
        """
        Validates a signal against historical stats in nt_pattern_stats.
        Formula: EV = (P_win * Profit) - ((1 - P_win) * Loss)
        """
        try:
            # Query nt_pattern_stats for the win rate of this pattern in this regime
            # Using raw SQL for precision in the proxy environment
            query = "SELECT win_rate FROM nt_pattern_stats WHERE regime = :regime AND pattern = :pattern LIMIT 1"
            result = await db_session.execute(query, {"regime": regime, "pattern": pattern})
            row = result.fetchone()
            
            if row:
                p_win = float(row[0])
            else:
                # Fallback to a conservative default if no stats exist
                p_win = 0.50 
                
            p_loss = 1.0 - p_win
            ev = (p_win * expected_profit) - (p_loss * expected_loss)
            rr = expected_profit / expected_loss if expected_loss > 0 else 0.0
            
            allowed = (ev > self.min_ev) and (rr >= self.min_rr)
            reason = None
            if not allowed:
                reason = f"EV={ev:.2f}, RR={rr:.2f} (Min EV={self.min_ev}, Min RR={self.min_rr})"
            
            return EVResult(p_win=p_win, ev=ev, rr=rr, allowed=allowed, reason=reason)
            
        except Exception as e:
            logging.error(f"EVEngine error: {e}")
            return EVResult(p_win=0.5, ev=0.0, rr=0.0, allowed=False, reason="EV_Calculation_Error")

    # ── Existing: validate_signal_sync (sync, with optional P_regime) ──

    def validate_signal_sync(self, db_session, regime: str, pattern: str, 
                            expected_profit: float, expected_loss: float,
                            trend_probs: Optional[Dict[str, float]] = None,
                            direction: Optional[str] = None) -> EVResult:
        """
        Synchronous wrapper for validate_signal to be used in sync contexts (like SignalFilter.evaluate).
        
        If trend_probs and direction are provided, uses real P_regime from HMM posteriors.
        If not, falls back to p_win=0.55 (backward compatible).
        """
        try:
            if trend_probs is not None and direction is not None:
                # Use real P_regime from HMM posteriors
                p_regime = self.compute_p_regime(trend_probs, direction)
                # Blend with default neutral sources (no imbalance/Markov in sync path)
                p_win = p_regime
            else:
                # Backward compatible: conservative default
                p_win = 0.55
                
            p_loss = 1.0 - p_win
            ev = (p_win * expected_profit) - (p_loss * expected_loss)
            rr = expected_profit / expected_loss if expected_loss > 0 else 0.0
            
            allowed = (ev > self.min_ev) and (rr >= self.min_rr)
            reason = None
            if not allowed:
                reason = f"EV={ev:.2f}, RR={rr:.2f} (Min EV={self.min_ev}, Min RR={self.min_rr})"
            
            return EVResult(p_win=p_win, ev=ev, rr=rr, allowed=allowed, reason=reason)
            
        except Exception as e:
            logging.error(f"EVEngine sync error: {e}")
            return EVResult(p_win=0.5, ev=0.0, rr=0.0, allowed=False, reason="EV_Sync_Calculation_Error")

    # ── v3: Regime Shift Meta-Signal ──────────────────────────────

    @staticmethod
    def _shift_direction(prev_regime: str, current_regime: str) -> str:
        """
        Determine the direction of a regime shift.

        Returns ``"bullish"``, ``"bearish"``, or ``"neutral"`` based on
        the presence of bull/bear labels in the regime strings.
        """
        bull_markers = {"bull", "strong_bull"}
        bear_markers = {"bear", "strong_bear"}

        prev_bull = any(m in prev_regime for m in bull_markers)
        prev_bear = any(m in prev_regime for m in bear_markers)
        curr_bull = any(m in current_regime for m in bull_markers)
        curr_bear = any(m in current_regime for m in bear_markers)

        if not prev_bull and curr_bull:
            return "bullish"
        if not prev_bear and curr_bear:
            return "bearish"
        if prev_bull and not curr_bull:
            return "bearish"
        if prev_bear and not curr_bear:
            return "bullish"
        return "neutral"

    def validate_regime_shift(
        self,
        hmm_regime: str,
        prev_regime: str,
        p_timesfm: Optional[float] = None,
        timesfm_confidence: float = 0.0,
    ) -> tuple:
        """
        Validate an HMM regime shift against the TimesFM forecast.

        When TimesFM disagrees with the HMM's detected shift direction,
        the shift is marked as *unconfirmed* — the caller should
        preserve the previous regime rather than switching.

        Parameters
        ----------
        hmm_regime : str
            Regime label detected by HMM (e.g. ``"high_vol_bear"``).
        prev_regime : str
            The regime before this detection cycle.
        p_timesfm : float or None
            TimesFM forecast probability (0–1). ``None`` = no signal.
        timesfm_confidence : float
            TimesFM model's own confidence in its forecast (0–1).

        Returns
        -------
        (confirmed: bool, reason: str)
            ``(True, "confirmed_...")`` or ``(False, "unconfirmed_...")``
        """
        # No shift detected — always confirmed (nothing to validate)
        if hmm_regime == prev_regime:
            return (True, "no_shift")

        # Without TimesFM signal, trust HMM
        if p_timesfm is None or timesfm_confidence < TIMESFM_CONFIDENCE_MIN:
            return (True, f"trust_hmm_shift_to_{hmm_regime}")

        shift_dir = self._shift_direction(prev_regime, hmm_regime)

        if shift_dir == "bullish" and p_timesfm >= 0.5:
            return (True, f"confirmed_bullish_shift_to_{hmm_regime}")
        elif shift_dir == "bearish" and p_timesfm <= 0.5:
            return (True, f"confirmed_bearish_shift_to_{hmm_regime}")
        elif shift_dir == "neutral":
            # Volatility-only shift (e.g. low_vol → high_vol) — trust HMM
            return (True, f"confirmed_vol_shift_to_{hmm_regime}")
        else:
            # HMM says shift, TimesFM disagrees
            return (
                False,
                f"unconfirmed_shift_hmm={hmm_regime}_timesfm={p_timesfm:.2f}",
            )

    # ── v3: Tail Risk Assessment ──────────────────────────────

    @staticmethod
    def compute_tail_risk(
        p_timesfm: Optional[float],
        regime: str,
        timesfm_confidence: float = 0.0,
    ) -> dict:
        """
        Assess tail risk from extreme TimesFM forecasts.

        When TimesFM is extremely confident in one direction during a
        volatile regime, it may be detecting a tail event (flash crash,
        regime break). Returns a risk score and position multiplier.

        Returns
        -------
        dict with keys: score (0-1), action (str), multiplier (float)
        """
        result = {"score": 0.0, "action": "none", "multiplier": 1.0}

        if p_timesfm is None or timesfm_confidence < 0.5:
            return result

        is_extreme = p_timesfm < 0.2 or p_timesfm > 0.8
        is_high_vol = "high_vol" in regime
        is_extreme_confident = is_extreme and timesfm_confidence >= 0.7

        if is_extreme_confident and is_high_vol:
            result["score"] = 0.85
            result["action"] = "skip"
            result["multiplier"] = 0.0
        elif is_extreme_confident:
            result["score"] = 0.60
            result["action"] = "reduce_50"
            result["multiplier"] = 0.5
        elif is_extreme and is_high_vol:
            result["score"] = 0.35
            result["action"] = "reduce_25"
            result["multiplier"] = 0.75

        return result

    # ── Existing: sync_journal_to_stats ───────────────────────────

    async def sync_journal_to_stats(self, db_session):
        """
        Extracts historical trade data from the active TradeJournal memory
        and upserts aggregated win rates/PnL into nt_pattern_stats.
        """
        try:
            from renko.trade_journal import TradeJournal
            journal = TradeJournal() 
            trades = list(journal.trades)
            
            if not trades:
                return {"status": "empty", "message": "No trades found in active journal memory."}

            stats_map = {}
            for t in trades:
                regime = t.regime or "unknown"
                pattern = t.pattern_type.value if hasattr(t.pattern_type, 'value') else str(t.pattern_type)
                key = (regime, pattern)
                
                if key not in stats_map:
                    stats_map[key] = {"wins": 0, "total": 0, "sum_pnl": 0.0}
                
                stats_map[key]["total"] += 1
                if t.is_win:
                    stats_map[key]["wins"] += 1
                stats_map[key]["sum_pnl"] += t.pnl_bricks

            from sqlalchemy import text
            count = 0
            for (regime, pattern), data in stats_map.items():
                win_rate = data["wins"] / data["total"]
                avg_pnl = data["sum_pnl"] / data["total"]
                
                query = text("""
                    INSERT INTO nt_pattern_stats (regime, pattern, win_rate, total_trades, avg_pnl)
                    VALUES (:regime, :pattern, :win_rate, :total, :avg_pnl)
                    ON CONFLICT (regime, pattern) 
                    DO UPDATE SET 
                        win_rate = EXCLUDED.win_rate,
                        total_trades = EXCLUDED.total_trades,
                        avg_pnl = EXCLUDED.avg_pnl;
                """)
                db_session.execute(query, {
                    "regime": regime, 
                    "pattern": pattern, 
                    "win_rate": win_rate, 
                    "total": data["total"], 
                    "avg_pnl": avg_pnl
                })
                count += 1
            
            db_session.commit()
            return {"status": "success", "synced_pairs": count, "total_trades_processed": len(trades)}

        except Exception as e:
            return {"status": "error", "message": str(e)}
