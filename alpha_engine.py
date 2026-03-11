"""Alpha-Scalp Bot – Alpha Engine Module (Phase 1).

Converts raw feature values into small directional "votes" ranging from
-2 (strong short) to +2 (strong long).  Each signal function reads from
the FeatureSet (computed once by FeatureCache) and returns a vote.

The votes are later aggregated by SignalScoring into a single weighted
score that determines whether to trade and at what confidence.

Signal Votes:
    +2  strong long   (e.g. RSI < 25, very oversold)
    +1  mild long     (e.g. RSI 25-35, somewhat oversold)
     0  neutral       (no edge)
    -1  mild short    (e.g. RSI 65-75, somewhat overbought)
    -2  strong short  (e.g. RSI > 75, very overbought)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

import config as cfg
from feature_cache import FeatureSet


@dataclass
class AlphaVotes:
    """Container for all alpha signal votes."""

    ema_cross: int = 0       # EMA crossover direction
    ema_trend: int = 0       # EMA trend alignment
    rsi: int = 0             # RSI momentum
    nw_envelope: int = 0     # Nadaraya-Watson mean reversion
    volume: int = 0          # Volume confirmation
    bb_squeeze: int = 0      # Bollinger Band squeeze breakout
    adx_regime: int = 0      # ADX trend strength
    cvd: int = 0              # Cumulative Volume Delta pressure

    def as_dict(self) -> dict[str, int]:
        """Return all votes as a dictionary."""
        return {
            k: getattr(self, k)
            for k in self.__dataclass_fields__
        }

    def total(self) -> int:
        """Sum of all raw votes (unweighted)."""
        return sum(self.as_dict().values())

    @property
    def signal_names(self) -> list[str]:
        """Names of all signals that contributed a non-zero vote."""
        return [k for k, v in self.as_dict().items() if v != 0]


class AlphaEngine:
    """Convert FeatureSet values into directional votes (-2 to +2).

    Each method below is a self-contained signal that reads from the
    cached FeatureSet and returns a vote.  New signals can be added
    by simply writing a new ``_vote_xxx`` method and including it
    in ``generate_votes()``.
    """

    def __init__(self) -> None:
        logger.info("AlphaEngine initialised (8 signal voters)")

    # ------------------------------------------------------------------
    # Individual signal voters
    # ------------------------------------------------------------------

    @staticmethod
    def _vote_ema_cross(fs: FeatureSet) -> int:
        """EMA crossover: +2 cross up, -2 cross down, else 0."""
        if fs.ema_cross_up:
            return +2
        if fs.ema_cross_down:
            return -2
        return 0

    @staticmethod
    def _vote_ema_trend(fs: FeatureSet) -> int:
        """EMA trend alignment: +1 bullish, -1 bearish.

        Not as strong as a fresh cross, but confirms direction.
        """
        return fs.ema_trend  # already +1, 0, or -1

    @staticmethod
    def _vote_rsi(fs: FeatureSet) -> int:
        """RSI momentum vote.

        Zones:
            RSI < 25       → +2 (deeply oversold, strong long)
            RSI 25-35      → +1 (oversold, mild long)
            RSI 35-65      →  0 (neutral)
            RSI 65-75      → -1 (overbought, mild short)
            RSI > 75       → -2 (deeply overbought, strong short)
        """
        rsi = fs.rsi
        if rsi < 25:
            return +2
        if rsi < 35:
            return +1
        if rsi > 75:
            return -2
        if rsi > 65:
            return -1
        return 0

    @staticmethod
    def _vote_nw_envelope(fs: FeatureSet) -> int:
        """Nadaraya-Watson envelope mean-reversion vote.

        Price crossing below lower band = mean reversion long (+2)
        Price crossing above upper band = mean reversion short (-2)
        Price near lower band (within 0.3%) = mild long (+1)
        Price near upper band (within 0.3%) = mild short (-1)
        """
        if fs.nw_long_cross:
            return +2
        if fs.nw_short_cross:
            return -2

        # Near-band proximity (within 0.3% of band)
        band_range = fs.nw_upper - fs.nw_lower
        if band_range > 0:
            lower_dist = (fs.close - fs.nw_lower) / band_range
            upper_dist = (fs.nw_upper - fs.close) / band_range
            if lower_dist < 0.1:  # within 10% of band range from lower
                return +1
            if upper_dist < 0.1:  # within 10% of band range from upper
                return -1
        return 0

    @staticmethod
    def _vote_volume(fs: FeatureSet) -> int:
        """Volume confirmation vote.

        Volume spike (>1.5x) confirms the current direction.
        Without volume, the signal is weaker (0).

        2x+ avg  → +2 (strong confirmation in EMA direction)
        1.5-2x   → +1 (moderate confirmation)
        < 1.5x   →  0 (no volume edge)
        """
        if not fs.volume_spike:
            return 0
        # Volume confirms direction — sign comes from EMA trend
        direction = fs.ema_trend if fs.ema_trend != 0 else (1 if fs.rsi < 50 else -1)
        if fs.volume_ratio >= 2.0:
            return +2 * direction
        return +1 * direction

    @staticmethod
    def _vote_bb_squeeze(fs: FeatureSet) -> int:
        """Bollinger Band squeeze vote.

        Squeeze = low volatility compression → breakout imminent.
        Direction comes from EMA trend.

        Squeeze active → +1 in EMA direction (breakout anticipated)
        No squeeze     →  0
        """
        if not fs.bb_squeeze:
            return 0
        direction = fs.ema_trend if fs.ema_trend != 0 else 0
        return +1 * direction

    @staticmethod
    def _vote_adx_regime(fs: FeatureSet) -> int:
        """ADX regime vote.

        Strong trend (ADX > 40) → +1 in EMA direction (ride the trend)
        Trending (25-40)        →  0 (neutral, trend exists but not extreme)
        Ranging (< 25)          → invert EMA direction by +1 (mean reversion)
        """
        if fs.regime == "VOLATILE":
            # Strong trend — go with the trend
            return +1 * fs.ema_trend
        if fs.regime == "RANGING":
            # Mean reversion — fade the trend
            return -1 * fs.ema_trend
        return 0  # TRENDING = neutral boost

    @staticmethod
    def _vote_cvd(fs: FeatureSet) -> int:
        """Cumulative Volume Delta vote.

        CVD measures net buying vs selling pressure.  Uses three sub-signals:

        1. CVD slope (momentum of order flow):
           slope > +strong_threshold  → +2 (aggressive buying)
           slope > +mild_threshold    → +1 (mild buying)
           slope < -strong_threshold  → -2 (aggressive selling)
           slope < -mild_threshold    → -1 (mild selling)

        2. CVD divergence (bonus):
           Price falling + CVD rising  → +1 (hidden bullish pressure)
           Price rising  + CVD falling → -1 (hidden bearish pressure)

        The two sub-signals are summed and clamped to [-2, +2].
        """
        if not getattr(cfg, "CVD_ENABLED", False):
            return 0

        strong = getattr(cfg, "CVD_STRONG_THRESHOLD", 0.6)
        mild = getattr(cfg, "CVD_MILD_THRESHOLD", 0.3)
        slope = fs.cvd_slope

        # Sub-signal 1: slope direction
        vote = 0
        if slope >= strong:
            vote = +2
        elif slope >= mild:
            vote = +1
        elif slope <= -strong:
            vote = -2
        elif slope <= -mild:
            vote = -1

        # Sub-signal 2: divergence bonus
        vote += fs.cvd_divergence  # +1, 0, or -1

        # Clamp to [-2, +2]
        return max(-2, min(+2, vote))

    # ------------------------------------------------------------------
    # Main vote generator
    # ------------------------------------------------------------------

    def generate_votes(self, fs: FeatureSet) -> AlphaVotes:
        """Run all signal voters against the current FeatureSet.

        Parameters
        ----------
        fs : FeatureSet
            The cached feature snapshot for the current bar.

        Returns
        -------
        AlphaVotes
            Container with each signal's vote.
        """
        votes = AlphaVotes(
            ema_cross=self._vote_ema_cross(fs),
            ema_trend=self._vote_ema_trend(fs),
            rsi=self._vote_rsi(fs),
            nw_envelope=self._vote_nw_envelope(fs),
            volume=self._vote_volume(fs),
            bb_squeeze=self._vote_bb_squeeze(fs),
            adx_regime=self._vote_adx_regime(fs),
            cvd=self._vote_cvd(fs),
        )

        logger.debug(
            "AlphaEngine votes | total={:+d} | {}",
            votes.total(),
            " ".join(f"{k}={v:+d}" for k, v in votes.as_dict().items() if v != 0),
        )
        return votes
