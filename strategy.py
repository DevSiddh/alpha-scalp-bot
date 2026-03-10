"""Alpha-Scalp Bot – Trading Strategy Module.

Signal generation pipeline:
1. EMA 9/21 crossover detection
2. RSI 14 momentum filter
3. Nadaraya-Watson Gaussian kernel envelope for mean-reversion context

A trade signal fires only when ALL three conditions align.

Note: This module is exchange-agnostic. It operates purely on OHLCV
DataFrames and does not make any exchange API calls directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd
import pandas_ta as ta
from loguru import logger

import config as cfg


# ---------------------------------------------------------------------------
# Signal types
# ---------------------------------------------------------------------------
class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TradeSignal:
    """Immutable container returned by :meth:`ScalpStrategy.calculate_signals`."""

    signal: Signal
    confidence: float  # 0.0 – 1.0
    entry_price: float
    ema_fast: float
    ema_slow: float
    rsi: float
    nw_mid: float
    nw_upper: float
    nw_lower: float
    reason: str


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
class ScalpStrategy:
    """RSI + EMA crossover + Nadaraya-Watson Envelope scalp strategy."""

    def __init__(self) -> None:
        # EMA
        self.ema_fast_period: int = cfg.EMA_FAST
        self.ema_slow_period: int = cfg.EMA_SLOW

        # RSI
        self.rsi_period: int = cfg.RSI_PERIOD
        self.rsi_oversold: int = cfg.RSI_OVERSOLD
        self.rsi_overbought: int = cfg.RSI_OVERBOUGHT

        # Nadaraya-Watson
        self.nw_bandwidth: float = cfg.NW_BANDWIDTH
        self.nw_mult: float = cfg.NW_MULT
        self.nw_lookback: int = cfg.NW_LOOKBACK

        logger.info(
            "ScalpStrategy initialised | EMA {}/{} | RSI {} ({}/<{}) | "
            "NW bw={} mult={} lb={}",
            self.ema_fast_period,
            self.ema_slow_period,
            self.rsi_period,
            self.rsi_oversold,
            self.rsi_overbought,
            self.nw_bandwidth,
            self.nw_mult,
            self.nw_lookback,
        )

    # -----------------------------------------------------------------
    # Nadaraya-Watson Gaussian Kernel Regression
    # -----------------------------------------------------------------
    @staticmethod
    def nadaraya_watson_envelope(
        close_prices: np.ndarray,
        bandwidth: float,
        mult: float,
        lookback: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Compute Nadaraya-Watson kernel regression with Gaussian kernel.

        The Nadaraya-Watson estimator at point *i* is:

            y_hat(i) = sum_j[ K((i-j)/h) * y_j ] / sum_j[ K((i-j)/h) ]

        where K(u) = exp(-u^2 / 2)  (Gaussian kernel) and *h* is the
        bandwidth parameter.

        The envelope is formed by adding/subtracting ``mult * std`` of
        the residuals within the lookback window.

        Parameters
        ----------
        close_prices : np.ndarray
            1-D array of close prices.
        bandwidth : float
            Kernel bandwidth *h* – controls smoothness.
        mult : float
            Multiplier for the standard-deviation envelope.
        lookback : int
            Number of recent bars used for the kernel window.

        Returns
        -------
        tuple[np.ndarray, np.ndarray, np.ndarray]
            ``(nw_mid, nw_upper, nw_lower)`` arrays of the same length
            as *close_prices*.  Leading values where the lookback is
            insufficient are filled with ``NaN``.
        """
        n = len(close_prices)
        nw_mid = np.full(n, np.nan)
        nw_upper = np.full(n, np.nan)
        nw_lower = np.full(n, np.nan)

        if n < lookback:
            logger.warning(
                "Not enough data for NW envelope ({} < {})", n, lookback
            )
            return nw_mid, nw_upper, nw_lower

        for i in range(lookback - 1, n):
            # Window indices
            start = i - lookback + 1
            window = close_prices[start : i + 1]
            m = len(window)

            # Kernel weights: K((i - j) / h) for j in window
            # j ranges from 0..m-1, distance from current point = m-1-j
            distances = np.arange(m - 1, -1, -1, dtype=np.float64)
            weights = np.exp(-(distances ** 2) / (2 * bandwidth ** 2))

            weight_sum = weights.sum()
            if weight_sum == 0:
                continue

            # Kernel regression estimate
            y_hat = np.dot(weights, window) / weight_sum
            nw_mid[i] = y_hat

            # Weighted residual standard deviation
            residuals = window - y_hat
            weighted_var = np.dot(weights, residuals ** 2) / weight_sum
            std = np.sqrt(weighted_var)

            nw_upper[i] = y_hat + mult * std
            nw_lower[i] = y_hat - mult * std

        return nw_mid, nw_upper, nw_lower

    # -----------------------------------------------------------------
    # EMA cross detection
    # -----------------------------------------------------------------
    @staticmethod
    def _detect_cross(
        fast: pd.Series, slow: pd.Series
    ) -> tuple[bool, bool]:
        """Return ``(cross_above, cross_below)`` on the latest bar.

        A cross is detected when fast and slow swap relative position
        between the previous bar and the current bar.
        """
        if len(fast) < 2 or len(slow) < 2:
            return False, False

        prev_fast, curr_fast = fast.iloc[-2], fast.iloc[-1]
        prev_slow, curr_slow = slow.iloc[-2], slow.iloc[-1]

        cross_above = (prev_fast <= prev_slow) and (curr_fast > curr_slow)
        cross_below = (prev_fast >= prev_slow) and (curr_fast < curr_slow)
        return cross_above, cross_below

    # -----------------------------------------------------------------
    # Main signal generator
    # -----------------------------------------------------------------
    def calculate_signals(self, df: pd.DataFrame) -> TradeSignal:
        """Analyse an OHLCV DataFrame and return a :class:`TradeSignal`.

        Required columns: ``open, high, low, close, volume``.

        Signal logic
        ------------
        **BUY**  – EMA9 crosses above EMA21 *and* RSI < 35
                   *and* close <= NW lower band (mean-reversion entry).

        **SELL** – EMA9 crosses below EMA21 *and* RSI > 65
                   *and* close >= NW upper band.

        Otherwise **HOLD**.
        """
        if df is None or len(df) < self.ema_slow_period + 2:
            logger.warning("Insufficient data for signal calculation")
            return TradeSignal(
                signal=Signal.HOLD,
                confidence=0.0,
                entry_price=0.0,
                ema_fast=0.0,
                ema_slow=0.0,
                rsi=0.0,
                nw_mid=0.0,
                nw_upper=0.0,
                nw_lower=0.0,
                reason="Insufficient data",
            )

        # --- Indicators ------------------------------------------------
        close = df["close"].astype(float)

        ema_fast = ta.ema(close, length=self.ema_fast_period)
        ema_slow = ta.ema(close, length=self.ema_slow_period)
        rsi = ta.rsi(close, length=self.rsi_period)

        nw_mid, nw_upper, nw_lower = self.nadaraya_watson_envelope(
            close.values, self.nw_bandwidth, self.nw_mult, self.nw_lookback
        )

        # Latest values
        curr_close = float(close.iloc[-1])
        curr_ema_fast = float(ema_fast.iloc[-1]) if ema_fast is not None else 0.0
        curr_ema_slow = float(ema_slow.iloc[-1]) if ema_slow is not None else 0.0
        curr_rsi = float(rsi.iloc[-1]) if rsi is not None else 50.0
        curr_nw_mid = float(nw_mid[-1]) if not np.isnan(nw_mid[-1]) else curr_close
        curr_nw_upper = float(nw_upper[-1]) if not np.isnan(nw_upper[-1]) else curr_close * 1.01
        curr_nw_lower = float(nw_lower[-1]) if not np.isnan(nw_lower[-1]) else curr_close * 0.99

        # --- Cross detection -------------------------------------------
        cross_above, cross_below = self._detect_cross(ema_fast, ema_slow)

        # --- NW band proximity -----------------------------------------
        # "near" = within 0.1 % of the band or beyond it
        nw_band_tolerance = 0.001  # 0.1 %
        near_lower = curr_close <= curr_nw_lower * (1 + nw_band_tolerance)
        near_upper = curr_close >= curr_nw_upper * (1 - nw_band_tolerance)

        # --- Signal decision -------------------------------------------
        signal = Signal.HOLD
        confidence = 0.0
        reasons: list[str] = []

        # BUY conditions
        buy_ema = cross_above
        buy_rsi = curr_rsi < 35
        buy_nw = near_lower

        # SELL conditions
        sell_ema = cross_below
        sell_rsi = curr_rsi > 65
        sell_nw = near_upper

        if buy_ema and buy_rsi and buy_nw:
            signal = Signal.BUY
            # Confidence: average of component strengths
            rsi_strength = (35 - curr_rsi) / 35  # stronger when lower
            nw_strength = max(
                0, (curr_nw_lower - curr_close) / (curr_nw_upper - curr_nw_lower + 1e-9)
            )
            confidence = min(1.0, 0.4 + 0.3 * rsi_strength + 0.3 * nw_strength)
            reasons = [
                f"EMA{self.ema_fast_period} crossed above EMA{self.ema_slow_period}",
                f"RSI={curr_rsi:.1f} < 35 (oversold zone)",
                f"Price {curr_close:.2f} near/below NW lower {curr_nw_lower:.2f}",
            ]

        elif sell_ema and sell_rsi and sell_nw:
            signal = Signal.SELL
            rsi_strength = (curr_rsi - 65) / 35
            nw_strength = max(
                0, (curr_close - curr_nw_upper) / (curr_nw_upper - curr_nw_lower + 1e-9)
            )
            confidence = min(1.0, 0.4 + 0.3 * rsi_strength + 0.3 * nw_strength)
            reasons = [
                f"EMA{self.ema_fast_period} crossed below EMA{self.ema_slow_period}",
                f"RSI={curr_rsi:.1f} > 65 (overbought zone)",
                f"Price {curr_close:.2f} near/above NW upper {curr_nw_upper:.2f}",
            ]

        else:
            reasons = ["No confluence \u2013 HOLD"]

        reason_str = " | ".join(reasons)

        trade_signal = TradeSignal(
            signal=signal,
            confidence=round(confidence, 3),
            entry_price=curr_close,
            ema_fast=round(curr_ema_fast, 2),
            ema_slow=round(curr_ema_slow, 2),
            rsi=round(curr_rsi, 2),
            nw_mid=round(curr_nw_mid, 2),
            nw_upper=round(curr_nw_upper, 2),
            nw_lower=round(curr_nw_lower, 2),
            reason=reason_str,
        )

        if signal != Signal.HOLD:
            logger.info(
                "SIGNAL {} | conf={:.1%} | {}",
                signal.value,
                confidence,
                reason_str,
            )
        else:
            logger.debug(
                "HOLD | EMA_x_up={} EMA_x_dn={} RSI={:.1f} "
                "close={:.2f} NW=[{:.2f}, {:.2f}]",
                cross_above,
                cross_below,
                curr_rsi,
                curr_close,
                curr_nw_lower,
                curr_nw_upper,
            )

        return trade_signal
