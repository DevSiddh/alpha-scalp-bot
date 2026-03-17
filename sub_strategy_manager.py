"""Alpha-Scalp Bot – SubStrategyManager.

GP-S5: Selects which of 5 sub-strategies (or Cash Mode) is active on each
candle, and enforces two hard gates before any trade is allowed:

  Hard Gate 1 — Microstructure gate:
      At least one of ob_imbalance or trade_aggression_ratio must be
      outside its neutral band.  Neutral ob_imbalance = 0.35–0.65;
      neutral aggression = 0.40–0.60.

  Hard Gate 2 — Swing-bias gate:
      If swing_bias has a directional vote that opposes the proposed
      action, the trade is blocked regardless of score.

Sub-strategies (in priority order):
  1. LiquiditySweepReversal  — liquidity_sweep required, any regime
  2. Breakout                — bb_squeeze in trending/volatile regime
  3. TrendPullback           — mtf_bias in trending regime
  4. VWAP_MeanReversion      — vwap_cross in ranging/neutral regime
  5. OrderFlowMomentum       — trade_aggression, any regime
  6. Cash                    — fallback (no trade)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from alpha_engine import AlphaVotes
    from feature_cache import FeatureSet


# ---------------------------------------------------------------------------
# Sub-strategy definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubStrategy:
    """Immutable descriptor for one sub-strategy."""
    name: str
    category: str          # breakout | mean_reversion | sweep_reversal |
                           # trend_pullback | order_flow | cash
    weight_multipliers: dict[str, float] = field(
        default_factory=dict, compare=False, hash=False
    )
    required_signal: str | None = None   # MUST have non-HOLD vote (or strategy skipped)
    preferred_regimes: tuple[str, ...] = ()  # empty = all regimes OK
    is_cash: bool = False


# ---------------------------------------------------------------------------
# Strategy catalogue
# ---------------------------------------------------------------------------

CASH_STRATEGY: SubStrategy = SubStrategy(
    name="Cash", category="cash", is_cash=True,
)

_STRATEGIES: list[SubStrategy] = [
    # Priority 1 — sweep reversal fires whenever sweep is detected (any regime)
    SubStrategy(
        name="LiquiditySweepReversal",
        category="sweep_reversal",
        weight_multipliers={
            "liquidity_sweep":  3.0,
            "trade_aggression": 1.5,
            "ob_imbalance":     1.5,
        },
        required_signal="liquidity_sweep",
        preferred_regimes=(),
    ),
    # Priority 2 — breakout: BB squeeze in trending/volatile
    SubStrategy(
        name="Breakout",
        category="breakout",
        weight_multipliers={
            "bb_squeeze":   2.0,
            "volume_spike": 1.5,
            "ema_cross":    1.5,
            "adx_filter":   1.2,
        },
        required_signal="bb_squeeze",
        preferred_regimes=("TRENDING_UP", "TRENDING_DOWN", "VOLATILE"),
    ),
    # Priority 3 — trend pullback in trending regime
    SubStrategy(
        name="TrendPullback",
        category="trend_pullback",
        weight_multipliers={
            "mtf_bias":   2.0,
            "swing_bias": 2.0,
            "ema_cross":  1.5,
            "rsi_zone":   1.3,
        },
        required_signal="mtf_bias",
        preferred_regimes=("TRENDING_UP", "TRENDING_DOWN"),
    ),
    # Priority 4 — mean reversion in ranging/neutral
    SubStrategy(
        name="VWAP_MeanReversion",
        category="mean_reversion",
        weight_multipliers={
            "vwap_cross":   2.0,
            "rsi_zone":     1.5,
            "bb_bounce":    1.8,
            "ob_imbalance": 1.3,
        },
        required_signal="vwap_cross",
        preferred_regimes=("RANGING", "NEUTRAL"),
    ),
    # Priority 5 — order flow momentum (any regime, last real option)
    SubStrategy(
        name="OrderFlowMomentum",
        category="order_flow",
        weight_multipliers={
            "trade_aggression": 2.0,
            "ob_imbalance":     1.8,
            "funding_bias":     1.2,
            "obv_trend":        1.0,
        },
        required_signal="trade_aggression",
        preferred_regimes=(),
    ),
]


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class SubStrategyManager:
    """Selects the active sub-strategy and enforces both hard gates."""

    # Microstructure gate thresholds
    OB_NEUTRAL_LOW:  float = 0.35
    OB_NEUTRAL_HIGH: float = 0.65
    AGG_NEUTRAL_LOW:  float = 0.40
    AGG_NEUTRAL_HIGH: float = 0.60

    # ── Hard Gate 1: Microstructure ─────────────────────────────────────────

    def check_microstructure_gate(self, features: "FeatureSet") -> bool:
        """Return True (passes) if at least one order-flow reading is non-neutral.

        Non-neutral means:
          ob_imbalance  < 0.35  or  > 0.65
          trade_aggression_ratio < 0.40  or  > 0.60
        """
        ob = getattr(features, "ob_imbalance", 0.5)
        ag = getattr(features, "trade_aggression_ratio", 0.5)

        ob_active = ob < self.OB_NEUTRAL_LOW or ob > self.OB_NEUTRAL_HIGH
        ag_active = ag < self.AGG_NEUTRAL_LOW or ag > self.AGG_NEUTRAL_HIGH

        passed = ob_active or ag_active
        if not passed:
            logger.debug(
                "Microstructure gate BLOCKED | ob={:.3f} ag={:.3f} (both neutral)",
                ob, ag,
            )
        return passed

    # ── Hard Gate 2: Swing-bias ─────────────────────────────────────────────

    def check_swing_bias_gate(self, votes: "AlphaVotes", action: str) -> bool:
        """Return True (passes) unless swing_bias directly opposes *action*.

        HOLD action always passes (no direction to oppose).
        swing_bias=HOLD always passes (no directional conviction).
        """
        if action == "HOLD":
            return True

        swing = getattr(votes, "swing_bias", None)
        if swing is None:
            return True

        swing_dir = swing.direction
        if swing_dir == "HOLD":
            return True

        opposed = (
            (action == "BUY"  and swing_dir == "SELL") or
            (action == "SELL" and swing_dir == "BUY")
        )
        if opposed:
            logger.warning(
                "Swing-bias gate BLOCKED | proposed={} swing_bias={}",
                action, swing_dir,
            )
        return not opposed

    # ── Strategy selection ──────────────────────────────────────────────────

    def select(
        self,
        votes: "AlphaVotes",
        features: "FeatureSet",
    ) -> SubStrategy:
        """Return the best-fitting sub-strategy for the current candle.

        Returns CASH_STRATEGY when:
          - Microstructure gate fails, OR
          - No strategy's required_signal is active in a matching regime.

        Note: swing_bias gate is checked separately (after scoring) via
        check_swing_bias_gate(), because the proposed action is not known
        until SignalScoring runs.
        """
        # Hard Gate 1 — microstructure
        if not self.check_microstructure_gate(features):
            return CASH_STRATEGY

        regime = getattr(features, "regime", "RANGING")
        vote_dict = votes.as_dict() if hasattr(votes, "as_dict") else {}

        for strategy in _STRATEGIES:
            # Required signal must have a non-HOLD (non-zero) vote
            if strategy.required_signal:
                if vote_dict.get(strategy.required_signal, 0) == 0:
                    continue

            # Regime must match (empty tuple = any regime)
            if strategy.preferred_regimes and regime not in strategy.preferred_regimes:
                continue

            logger.debug(
                "SubStrategy selected: {} | regime={}",
                strategy.name, regime,
            )
            return strategy

        logger.debug("No strategy matched — Cash | regime={}", regime)
        return CASH_STRATEGY

    def get_weight_multipliers(self, strategy: SubStrategy) -> dict[str, float]:
        """Return weight multipliers for *strategy* (callers multiply against registry defaults)."""
        return dict(strategy.weight_multipliers)
