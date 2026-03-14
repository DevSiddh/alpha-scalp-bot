"""Alpha-Scalp Bot – Signal Scoring Module (Phase 1).

Aggregates AlphaVotes into a single weighted score, applies a threshold
to decide BUY / SELL / HOLD, and computes a confidence level for
position sizing.

Scoring Formula:
    weighted_score = sum(vote_i * weight_i)

Decision:
    score >= +3 → BUY
    score <= -3 → SELL
    else → HOLD

Confidence (for Phase 2 position sizing):
    confidence = min(abs(score) / 6, 1.0)
    Score 2 = 33% size, Score 4 = 66%, Score 6+ = 100%

Weights are loaded from weights.json (per-regime in Phase 2).
Default weights start at 1.0 for all signals.

P1-3: Volatility filter - returns HOLD if atr_ratio outside valid range.
P1-4: Regime-based signal disabling - zero-weights signals for current regime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

import config as cfg

from alpha_engine import AlphaVotes
from feature_cache import FeatureSet


# ===== Phase 1-4: Regime-Based Signal Filtering =============================

def is_signal_enabled(signal_name: str, regime: str) -> bool:
    """Check if a signal is enabled for the current market regime.

    Args:
        signal_name: Name of the signal (e.g., "bb_bounce", "ema_cross")
        regime: Market regime (e.g., "TRENDING_DOWN", "VOLATILE", "TRENDING_UP", "NEUTRAL")

    Returns:
        False if the signal is disabled for this regime, True otherwise
    """
    disabled_signals = cfg.DISABLED_SIGNALS_BY_REGIME.get(regime, [])
    return signal_name not in disabled_signals


# Default signal weights (all equal to start)
DEFAULT_WEIGHTS: dict[str, float] = {
    "ema_cross":        1.4,
    "rsi_zone":         1.2,
    "macd_cross":       1.2,
    "bb_bounce":        1.0,
    "bb_squeeze":       1.3,
    "vwap_cross":       1.1,
    "obv_trend":        0.9,
    "volume_spike":     1.0,
    "swing_bias":       1.6,
    "nw_signal":        1.2,
    "adx_filter":       1.0,
    "funding_bias":     0.8,
    "mtf_bias":         1.5,
    "ob_imbalance":     1.1,
    "trade_aggression": 1.3,
    "liquidity_wall":   0.8,
    "liquidity_sweep":  1.7,
}

SCORE_THRESHOLD: float = 3.0  # Minimum |score| to trigger a trade


@dataclass
class ScoringResult:
    """Output of the scoring engine."""

    # Decision
    action: str = "HOLD"  # BUY | SELL | HOLD
    score: float = 0.0  # Raw weighted score
    abs_score: float = 0.0  # Absolute score
    confidence: float = 0.0  # 0.0 - 1.0 (for position sizing)
    threshold: float = SCORE_THRESHOLD

    # Breakdown for logging & TradeLogger
    vote_details: dict[str, int] = None  # raw votes
    weight_details: dict[str, float] = None  # weights used
    weighted_breakdown: dict[str, float] = None  # vote * weight per signal
    contributing_signals: list[str] = None  # signals that contributed

    # Regime context
    regime: str = "RANGING"

    # Volatility filter (P1-3)
    volatility_filter_triggered: bool = False

    def __post_init__(self):
        if self.vote_details is None:
            self.vote_details = {}
        if self.weight_details is None:
            self.weight_details = {}
        if self.weighted_breakdown is None:
            self.weighted_breakdown = {}
        if self.contributing_signals is None:
            self.contributing_signals = []

    def as_dict(self) -> dict[str, Any]:
        """Serializable dictionary for TradeLogger."""
        return {
            "action": self.action,
            "score": round(self.score, 2),
            "abs_score": round(self.abs_score, 2),
            "confidence": round(self.confidence, 3),
            "threshold": self.threshold,
            "regime": self.regime,
            "volatility_filter_triggered": self.volatility_filter_triggered,
            "vote_details": self.vote_details,
            "weighted_breakdown": {
                k: round(v, 2) for k, v in self.weighted_breakdown.items()
            },
            "contributing_signals": self.contributing_signals,
        }


class SignalScoring:
    """Weighted scoring engine for alpha votes.

    Loads weights from a JSON file (supports per-regime weights in Phase 2).
    Falls back to DEFAULT_WEIGHTS if no file exists.
    """

    def __init__(self, weights_file: str = "weights.json") -> None:
        self._weights_path = Path(weights_file)
        self._weights: dict[str, float] = dict(DEFAULT_WEIGHTS)
        self._regime_weights: dict[str, dict[str, float]] = {}
        self._load_weights()
        logger.info(
            "SignalScoring initialised | threshold={} | {} signals weighted",
            SCORE_THRESHOLD, len(self._weights),
        )

    # ------------------------------------------------------------------
    # Weight management
    # ------------------------------------------------------------------

    def _load_weights(self) -> None:
        """Load weights from JSON file if it exists."""
        if not self._weights_path.exists():
            logger.info("No weights.json found — using defaults")
            self._save_weights()  # Create initial file
            return

        try:
            with open(self._weights_path, "r") as f:
                data = json.load(f)

            if "default" in data:
                # Phase 2 format: {"default": {...}, "TRENDING": {...}, ...}
                self._weights = data["default"]
                self._regime_weights = {
                    k: v for k, v in data.items() if k != "default"
                }
                logger.info("Loaded per-regime weights from {}", self._weights_path)
            else:
                # Simple format: {"ema_cross": 1.5, ...}
                self._weights = data
                logger.info("Loaded flat weights from {}", self._weights_path)

        except Exception as exc:
            logger.error("Failed to load weights: {} — using defaults", exc)
            self._weights = dict(DEFAULT_WEIGHTS)

    def _save_weights(self) -> None:
        """Persist current weights to JSON."""
        try:
            self._weights_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"default": self._weights}
            data.update(self._regime_weights)
            with open(self._weights_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug("Weights saved to {}", self._weights_path)
        except Exception as exc:
            logger.error("Failed to save weights: {}", exc)

    def get_weights_for_regime(self, regime: str) -> dict[str, float]:
        """Get weights for a specific regime, falling back to default."""
        if regime in self._regime_weights:
            return self._regime_weights[regime]
        return self._weights

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score(self, votes: AlphaVotes, features: FeatureSet) -> ScoringResult:
        """Compute weighted score from alpha votes.

        P1-3: Applies volatility filter - returns HOLD if atr_ratio is outside valid range.
        P1-4: Zero-weights disabled signals for current regime.

        Parameters
        ----------
        votes : AlphaVotes
            Raw votes from AlphaEngine.
        features : FeatureSet
            Cached features (used for regime context and volatility filter).

        Returns
        -------
        ScoringResult
            Full scoring output with breakdown.
        """
        atr_ratio = features.atr / features.atr_ma50 if features.atr_ma50 > 0 else 1.0
        if atr_ratio > cfg.ATR_RATIO_MAX or atr_ratio < cfg.ATR_RATIO_MIN:
            return ScoringResult(action="HOLD", volatility_filter_triggered=True, regime=features.regime)
        volatility_filter_triggered = False

        regime = features.regime
        weights = self.get_weights_for_regime(regime)
        vote_dict = votes.as_dict()

        # Compute weighted score
        weighted_breakdown: dict[str, float] = {}
        total_score = 0.0

        # P1-4: Zero-weight disabled signals for current regime
        disabled = getattr(cfg, 'DISABLED_SIGNALS_BY_REGIME', {}).get(regime, [])

        bull_score = 0.0
        bear_score = 0.0

        for signal_name, vote_value in vote_dict.items():
            w = weights.get(signal_name, 1.0)
            if signal_name in disabled:
                w = 0.0
                logger.debug("Signal {} disabled for regime {} (weight=0)", signal_name, regime)
            weighted_val = vote_value * w
            weighted_breakdown[signal_name] = weighted_val
            total_score += weighted_val
            
            if weighted_val > 0:
                bull_score += weighted_val
            elif weighted_val < 0:
                bear_score += abs(weighted_val)

        abs_score = abs(total_score)

        # Consensus check
        total_score_abs = bull_score + bear_score
        
        # Decision
        if total_score >= SCORE_THRESHOLD:
            action = "BUY"
        elif total_score <= -SCORE_THRESHOLD:
            action = "SELL"
        else:
            action = "HOLD"
            
        if total_score_abs > 0:
            consensus = max(bull_score, bear_score) / total_score_abs
            if consensus < getattr(cfg, 'CONSENSUS_THRESHOLD', 0.65):
                action = "HOLD"

        # Confidence for position sizing (Phase 2)
        # Scale: score 3 = 50%, score 4 = 67%, score 6 = 100%
        confidence = min(abs_score / 6.0, 1.0)

        # Contributing signals (non-zero votes)
        contributing = [
            f"{name}({vote_dict[name]:+d}×{weights.get(name, 1.0):.1f}={weighted_breakdown[name]:+.1f})"
            for name in vote_dict
            if vote_dict[name] != 0
        ]

        result = ScoringResult(
            action=action,
            score=round(total_score, 2),
            abs_score=round(abs_score, 2),
            confidence=round(confidence, 3),
            threshold=SCORE_THRESHOLD,
            vote_details=vote_dict,
            weight_details=dict(weights),
            weighted_breakdown=weighted_breakdown,
            contributing_signals=contributing,
            regime=regime,
            volatility_filter_triggered=volatility_filter_triggered,
        )

        if volatility_filter_triggered:
            logger.info(
                "SCORE HOLD (volatility filter) | atr_ratio={:.2f} | regime={}",
                atr_ratio, regime,
            )
        elif action != "HOLD":
            logger.info(
                "SCORE {} | score={:+.2f} (threshold={}) | conf={:.1%} | "
                "regime={} | signals: {}",
                action, total_score, SCORE_THRESHOLD, confidence,
                regime, ", ".join(contributing),
            )
        else:
            logger.debug(
                "SCORE HOLD | score={:+.2f} | regime={} | {}",
                total_score, regime,
                " ".join(f"{k}={v:+d}" for k, v in vote_dict.items() if v != 0) or "no signals",
            )

        top3 = sorted(weighted_breakdown.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
        logger.debug(f"Top contributors: {top3}")

        return result
