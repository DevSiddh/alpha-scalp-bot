"""Tests for P1-8: 15m MTF bias vote — _compute_mtf_vote."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swing_strategy import SwingStrategy


def _make_df_15m(trend: str, n: int = 60) -> pd.DataFrame:
    """Build a synthetic 15m close series that guarantees EMA8 vs EMA20 relationship.

    trend='up'   -> rising prices  -> EMA8 > EMA20
    trend='down' -> falling prices -> EMA8 < EMA20
    trend='flat' -> constant price -> EMA8 == EMA20
    """
    if trend == "up":
        closes = np.linspace(100.0, 120.0, n)
    elif trend == "down":
        closes = np.linspace(120.0, 100.0, n)
    else:  # flat
        closes = np.full(n, 100.0)

    idx = pd.date_range("2025-01-01", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": np.ones(n) * 1000.0,
        },
        index=idx,
    )


class TestMtfBiasVote:
    def setup_method(self):
        self.s = SwingStrategy()

    def test_buy_ema8_above_ema20_rsi_50(self):
        """Case 1: EMA8 > EMA20, RSI=50 (>45) -> BUY, strength=0.7"""
        df = _make_df_15m("up")
        vote = self.s._compute_mtf_vote(df, rsi_override=50.0)
        assert vote.direction == "BUY"
        assert vote.strength == pytest.approx(0.7)

    def test_sell_ema8_below_ema20_rsi_50(self):
        """Case 2: EMA8 < EMA20, RSI=50 (<55) -> SELL, strength=0.7"""
        df = _make_df_15m("down")
        vote = self.s._compute_mtf_vote(df, rsi_override=50.0)
        assert vote.direction == "SELL"
        assert vote.strength == pytest.approx(0.7)

    def test_hold_ema8_equals_ema20(self):
        """Case 3: EMA8 == EMA20 (flat) -> HOLD, strength=0.0"""
        df = _make_df_15m("flat")
        vote = self.s._compute_mtf_vote(df, rsi_override=50.0)
        assert vote.direction == "HOLD"
        assert vote.strength == pytest.approx(0.0)
