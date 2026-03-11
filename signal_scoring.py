"""Alpha-Scalp Bot – Signal Scoring Module (Phase 1).

Aggregates AlphaVotes into a single weighted score, applies a threshold
to decide BUY / SELL / HOLD, and computes a confidence level for
position sizing.

Scoring Formula:
    weighted_score = sum(vote_i * weight_i)

Decision:
    score >= +3  → BUY
    score <= -3  → SELL
    else         → HOLD

Confidence (for Phase 2 position sizing):
    confidence = min(abs(score) / 6, 1.0)
    Score 2 = 33% size, Score 4 = 66%, Score 6+ = 100%

Weights are loaded from weights.json (per-regime in Phase 2).
Default weights start at 1.0 for all signals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from alpha_engine import AlphaVotes
from feature_cache import FeatureSet


# Default signal weights (all equal to start)
DEFAULT_WEIGHTS: dict[str, float] = {
    "ema_cross": 1.5,      # Fresh crossover is a strong signal
    "ema_trend": 0.8,      # Trend alignment — supporting, not primary
    "rsi": 1.2,            # RSI extremes are reliable
    "nw_envelope": 1.3,    # NW mean reversion — proven in our bot
    "volume": 1.0,         # Volume confirmation
    "bb_squeeze": 0.7,     # Squeeze is anticipatory, not definitive
    "adx_regime": 0.8,     # Regime context
    "cvd": 1.1,            # CVD order flow — strong but unproven, slightly above 1.0
}

SCORE_THRESHOLD: float = 3.0  # Minimum |score| to trigger a trade


@dataclass
class ScoringResult:
    """Output of the scoring engine."""

    # Decision
    action: str = "HOLD"        # BUY | SELL | HOLD
    score: float = 0.0          # Raw weighted score
    abs_score: float = 0.0      # Absolute score
    confidence: float = 0.0     # 0.0 - 1.0 (for position sizing)
    threshold: float = SCORE_THRESHOLD

    # Breakdown for logging & TradeLogger
    vote_details: dict[str, int] = None       # raw votes
    weight_details: dict[str, float] = None   # weights used
    weighted_breakdown: dict[str, float] = None  # vote * weight per signal
    contributing_signals: list[str] = None    # signals that contributed

    # Regime context
    regime: str = "RANGING"

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

        Parameters
        ----------
        votes : AlphaVotes
            Raw votes from AlphaEngine.
        features : FeatureSet
            Cached features (used for regime context).

        Returns
        -------
        ScoringResult
            Full scoring output with breakdown.
        """
        regime = features.regime
        weights = self.get_weights_for_regime(regime)
        vote_dict = votes.as_dict()

        # Compute weighted score
        weighted_breakdown: dict[str, float] = {}
        total_score = 0.0

        for signal_name, vote_value in vote_dict.items():
            w = weights.get(signal_name, 1.0)
            weighted_val = vote_value * w
            weighted_breakdown[signal_name] = weighted_val
            total_score += weighted_val

        abs_score = abs(total_score)

        # Decision
        if total_score >= SCORE_THRESHOLD:
            action = "BUY"
        elif total_score <= -SCORE_THRESHOLD:
            action = "SELL"
        else:
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
        )

        if action != "HOLD":
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

        return result
