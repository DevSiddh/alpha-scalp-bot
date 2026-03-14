"""Alpha-Scalp Bot – Signal Scoring Module.

Aggregates AlphaVotes into a single weighted score, applies a threshold
to decide BUY / SELL / HOLD, and computes a confidence level for
position sizing.

Scoring Formula:
    weighted_score = sum(vote_i * weight_i)

Decision:
    score >= SCORE_THRESHOLD  → BUY
    score <= -SCORE_THRESHOLD → SELL
    else                      → HOLD

Confidence (for position sizing):
    confidence = min(abs(score) / 6, 1.0)

FIXED (2026-03-14):
- SCORE_THRESHOLD lowered to 2.0 (was 3.0) — backtest uses 1.8 override.
- CONSENSUS_THRESHOLD lowered to 0.55 (was 0.65) — was blocking all trades:
  with 17 signals mostly HOLD, max(bull,bear)/(bull+bear) never hit 0.65.
- Consensus filter now only applies when total_score_abs > 1.0 (ignore
  noise from near-zero scores).
- Backtest-mode threshold override: if BACKTEST_SCORE_THRESHOLD set on
  the instance, uses that instead of module-level SCORE_THRESHOLD.
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


# ── Regime-Based Signal Filtering ───────────────────────────────────────────

def is_signal_enabled(signal_name: str, regime: str) -> bool:
    disabled_signals = cfg.DISABLED_SIGNALS_BY_REGIME.get(regime, [])
    return signal_name not in disabled_signals


# ── Default weights ──────────────────────────────────────────────────────────
# Updated 2026-03-14: rebalanced + new signals added.
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

# FIXED: lowered from 3.0 → 2.0
# Rationale: with 17 signals, most at HOLD, max realistic score when
# 4-5 signals agree is ~4-6. Threshold of 3.0 required near-consensus
# of all active signals. 2.0 fires when 2-3 strong signals agree.
SCORE_THRESHOLD: float = 2.0

# FIXED: lowered from 0.65 → 0.55
# Rationale: 0.65 means the winning side needs 65% of ALL weighted votes.
# With most signals at HOLD (contributing 0), the denominator is small
# but the threshold was still impossibly high. 0.55 is a meaningful
# majority without requiring unanimity.
CONSENSUS_THRESHOLD: float = 0.55


@dataclass
class ScoringResult:
    """Output of the scoring engine."""
    action: str = "HOLD"
    score: float = 0.0
    abs_score: float = 0.0
    confidence: float = 0.0
    threshold: float = SCORE_THRESHOLD
    vote_details: dict[str, int] = None
    weight_details: dict[str, float] = None
    weighted_breakdown: dict[str, float] = None
    contributing_signals: list[str] = None
    regime: str = "RANGING"
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

    Loads weights from weights.json (flat or per-regime format).
    Falls back to DEFAULT_WEIGHTS if no file exists.

    Parameters
    ----------
    weights_file : str
        Path to weights JSON file.
    score_threshold : float | None
        Override SCORE_THRESHOLD (used by backtest to lower threshold).
    consensus_threshold : float | None
        Override CONSENSUS_THRESHOLD.
    """

    def __init__(
        self,
        weights_file: str = "weights.json",
        score_threshold: float | None = None,
        consensus_threshold: float | None = None,
    ) -> None:
        self._weights_path = Path(weights_file)
        self._weights: dict[str, float] = dict(DEFAULT_WEIGHTS)
        self._regime_weights: dict[str, dict[str, float]] = {}
        self._score_threshold = score_threshold if score_threshold is not None else SCORE_THRESHOLD
        self._consensus_threshold = consensus_threshold if consensus_threshold is not None else CONSENSUS_THRESHOLD
        self._load_weights()
        logger.info(
            "SignalScoring initialised | score_threshold={} | consensus_threshold={} | {} signals",
            self._score_threshold, self._consensus_threshold, len(self._weights),
        )

    def _load_weights(self) -> None:
        if not self._weights_path.exists():
            logger.info("No weights.json found — using defaults")
            self._save_weights()
            return
        try:
            with open(self._weights_path, "r") as f:
                data = json.load(f)
            if "default" in data:
                self._weights = data["default"]
                self._regime_weights = {k: v for k, v in data.items() if k != "default"}
                logger.info("Loaded per-regime weights from {}", self._weights_path)
            else:
                self._weights = data
                logger.info("Loaded flat weights from {}", self._weights_path)
        except Exception as exc:
            logger.error("Failed to load weights: {} — using defaults", exc)
            self._weights = dict(DEFAULT_WEIGHTS)

    def _save_weights(self) -> None:
        try:
            self._weights_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._weights_path, "w") as f:
                json.dump(self._weights, f, indent=2)
            logger.debug("Weights saved to {}", self._weights_path)
        except Exception as exc:
            logger.error("Failed to save weights: {}", exc)

    def get_weights_for_regime(self, regime: str) -> dict[str, float]:
        if regime in self._regime_weights:
            return self._regime_weights[regime]
        return self._weights

    def score(self, votes: AlphaVotes, features: FeatureSet, debug: bool = False) -> ScoringResult:
        """Compute weighted score from alpha votes.

        Applies:
        - Volatility filter (ATR ratio gate)
        - Regime-based signal disabling
        - Consensus filter (fixed: lower threshold + noise guard)
        """
        # ── Volatility filter ────────────────────────────────────────────
        atr_ratio = features.atr / features.atr_ma50 if features.atr_ma50 > 0 else 1.0
        if atr_ratio > cfg.ATR_RATIO_MAX or atr_ratio < cfg.ATR_RATIO_MIN:
            if debug:
                logger.info(
                    "[SCORE] HOLD (volatility filter) | atr_ratio={:.2f} min={} max={}",
                    atr_ratio, cfg.ATR_RATIO_MIN, cfg.ATR_RATIO_MAX,
                )
            return ScoringResult(
                action="HOLD",
                volatility_filter_triggered=True,
                regime=features.regime,
            )
        volatility_filter_triggered = False

        regime = features.regime
        weights = self.get_weights_for_regime(regime)
        vote_dict = votes.as_dict()
        disabled = getattr(cfg, "DISABLED_SIGNALS_BY_REGIME", {}).get(regime, [])

        weighted_breakdown: dict[str, float] = {}
        total_score = 0.0
        bull_score = 0.0
        bear_score = 0.0

        for signal_name, vote_value in vote_dict.items():
            w = weights.get(signal_name, DEFAULT_WEIGHTS.get(signal_name, 1.0))
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
        total_score_abs = bull_score + bear_score

        threshold = self._score_threshold

        # ── Raw threshold decision ───────────────────────────────────────
        if total_score >= threshold:
            action = "BUY"
        elif total_score <= -threshold:
            action = "SELL"
        else:
            action = "HOLD"

        # ── Consensus filter (FIXED) ─────────────────────────────────────
        # Only apply when there is meaningful score to filter
        # (avoids flipping to HOLD on noise near-zero scores)
        consensus_blocked = False
        if action != "HOLD" and total_score_abs > 1.0:
            consensus = max(bull_score, bear_score) / total_score_abs
            cthresh = self._consensus_threshold
            if consensus < cthresh:
                if debug:
                    logger.info(
                        "[SCORE] Consensus filter blocked {} | consensus={:.3f} < threshold={}",
                        action, consensus, cthresh,
                    )
                action = "HOLD"
                consensus_blocked = True

        confidence = min(abs_score / 6.0, 1.0)

        contributing = [
            f"{name}({vote_dict[name]:+d}x{weights.get(name, 1.0):.1f}={weighted_breakdown[name]:+.1f})"
            for name in vote_dict
            if vote_dict[name] != 0
        ]

        result = ScoringResult(
            action=action,
            score=round(total_score, 2),
            abs_score=round(abs_score, 2),
            confidence=round(confidence, 3),
            threshold=threshold,
            vote_details=vote_dict,
            weight_details=dict(weights),
            weighted_breakdown=weighted_breakdown,
            contributing_signals=contributing,
            regime=regime,
            volatility_filter_triggered=volatility_filter_triggered,
        )

        # ── Logging ──────────────────────────────────────────────────────
        if debug:
            top3 = sorted(weighted_breakdown.items(), key=lambda x: abs(x[1]), reverse=True)[:5]
            logger.info(
                "[SCORE] {} | score={:+.2f} (thresh={}) | bull={:.2f} bear={:.2f} "
                "consensus={:.3f} | conf={:.1%} | regime={}",
                action, total_score, threshold,
                bull_score, bear_score,
                max(bull_score, bear_score) / total_score_abs if total_score_abs > 0 else 0,
                confidence, regime,
            )
            logger.info("[SCORE] Top contributors: {}", top3)
            if consensus_blocked:
                logger.info("[SCORE] -> Blocked by consensus filter")
            if contributing:
                logger.info("[SCORE] Active signals: {}", ", ".join(contributing))
        elif action != "HOLD":
            logger.info(
                "SCORE {} | score={:+.2f} (threshold={}) | conf={:.1%} | regime={} | signals: {}",
                action, total_score, threshold, confidence,
                regime, ", ".join(contributing),
            )
        else:
            logger.debug(
                "SCORE HOLD | score={:+.2f} | regime={} | {}",
                total_score, regime,
                " ".join(f"{k}={v:+d}" for k, v in vote_dict.items() if v != 0) or "no signals",
            )

        top3 = sorted(weighted_breakdown.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
        logger.debug("Top contributors: {}", top3)

        return result
