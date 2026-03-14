"""Alpha-Scalp Bot – Swing Trading Strategy Module.

Higher-timeframe strategy for 4h candles:
1. EMA 50/200 golden/death cross detection
2. RSI zone filter (40-50 for longs, 50-60 for shorts)
3. Support/Resistance from recent swing highs/lows (pivot window=5)
4. Volume confirmation
5. ATR calculation for dynamic stop-loss sizing
6. P1-8: 15m MTF confirmation via EMA(8/20) + RSI(14)

Designed to catch multi-day moves with wider SL/TP.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta
from loguru import logger

import config as cfg

# ---------------------------------------------------------------------------
# Shared Vote dataclass (frozen interface — do not change fields)
# ---------------------------------------------------------------------------

@dataclass
class Vote:
    direction: str   # "BUY" | "SELL" | "HOLD"
    strength: float  # 0.0 – 1.0
    reason: str


class SwingSignal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class SwingTradeSignal:
    """Container for swing trade signals."""
    signal: SwingSignal
    confidence: float
    entry_price: float
    symbol: str
    ema_fast: float
    ema_slow: float
    rsi: float
    support: float
    resistance: float
    reason: str
    atr: float
    mtf_vote: Optional[Vote] = None   # P1-8: populated when df_15m supplied


# ---------------------------------------------------------------------------
# MTF cache entry
# ---------------------------------------------------------------------------

_MTF_CACHE_TTL = 15 * 60  # 15 minutes in seconds


class SwingStrategy:
    """EMA 50/200 + RSI zones + Support/Resistance swing strategy."""

    def __init__(self) -> None:
        self.ema_fast_period: int = cfg.SWING_EMA_FAST
        self.ema_slow_period: int = cfg.SWING_EMA_SLOW
        self.rsi_period: int = cfg.SWING_RSI_PERIOD
        self.rsi_long_low: int = cfg.SWING_RSI_LONG_LOW
        self.rsi_long_high: int = cfg.SWING_RSI_LONG_HIGH
        self.rsi_short_low: int = cfg.SWING_RSI_SHORT_LOW
        self.rsi_short_high: int = cfg.SWING_RSI_SHORT_HIGH

        # P1-8: 15m MTF settings
        self._mtf_ema_fast: int = 8
        self._mtf_ema_slow: int = 20
        self._mtf_rsi_period: int = 14
        self._mtf_bars_required: int = self._mtf_ema_slow + 5  # 25 minimum

        # P1-8: 15m MTF in-memory cache
        # {"vote": Vote, "bar_ts": float, "cached_at": float}
        self._mtf_cache: dict = {}

        logger.info(
            "SwingStrategy initialised | EMA {}/{} | RSI {} (long {}-{}, short {}-{}) | TF={}",
            self.ema_fast_period,
            self.ema_slow_period,
            self.rsi_period,
            self.rsi_long_low,
            self.rsi_long_high,
            self.rsi_short_low,
            self.rsi_short_high,
            cfg.SWING_TIMEFRAME,
        )

    # ------------------------------------------------------------------
    # Support / Resistance
    # ------------------------------------------------------------------

    @staticmethod
    def find_support_resistance(
        df: pd.DataFrame, lookback: int = 50
    ) -> tuple[float, float]:
        """Find support/resistance from recent swing highs/lows."""
        if len(df) < lookback:
            lookback = len(df)

        recent = df.tail(lookback)
        highs = recent["high"].values
        lows = recent["low"].values

        swing_highs: list[float] = []
        swing_lows: list[float] = []
        pivot_window = 5

        for i in range(pivot_window, len(highs) - pivot_window):
            if highs[i] == max(highs[i - pivot_window: i + pivot_window + 1]):
                swing_highs.append(highs[i])
            if lows[i] == min(lows[i - pivot_window: i + pivot_window + 1]):
                swing_lows.append(lows[i])

        current_price = df["close"].iloc[-1]

        resistance_levels = [h for h in swing_highs if h > current_price]
        resistance = min(resistance_levels) if resistance_levels else highs.max()

        support_levels = [lo for lo in swing_lows if lo < current_price]
        support = max(support_levels) if support_levels else lows.min()

        return support, resistance

    # ------------------------------------------------------------------
    # P1-8: 15m MTF vote
    # ------------------------------------------------------------------

    def _compute_mtf_vote(self, df_15m: pd.DataFrame, rsi_override: float | None = None) -> Vote:
        """Compute the 15m MTF vote from EMA(8/20) and RSI(14).

        Args:
            df_15m: DataFrame of 15m OHLCV bars (close column required).
            rsi_override: If provided, substitute this value for the computed RSI
                          (used in unit tests to inject exact RSI values).

        Conditions (spec P1-8):
            EMA8 > EMA20 AND RSI > 45  -> BUY,  strength=0.7
            EMA8 < EMA20 AND RSI < 55  -> SELL, strength=0.7
            else                        -> HOLD, strength=0.0
        """
        close = df_15m["close"]

        ema_fast = float(close.ewm(span=self._mtf_ema_fast, adjust=False).mean().iloc[-1])
        ema_slow = float(close.ewm(span=self._mtf_ema_slow, adjust=False).mean().iloc[-1])

        if rsi_override is not None:
            rsi_val = float(rsi_override)
        else:
            rsi_series = ta.rsi(close, length=self._mtf_rsi_period)
            rsi_val = float(rsi_series.iloc[-1]) if rsi_series is not None and len(rsi_series) > 0 else 50.0

        if ema_fast > ema_slow and rsi_val > 45:
            return Vote(direction="BUY", strength=0.7, reason="15m MTF bullish")
        elif ema_fast < ema_slow and rsi_val < 55:
            return Vote(direction="SELL", strength=0.7, reason="15m MTF bearish")
        else:
            return Vote(direction="HOLD", strength=0.0, reason="15m MTF neutral")

    def get_mtf_vote(self, df_15m: pd.DataFrame | None) -> Vote:
        """Return cached 15m MTF vote, refreshing only on new bar close.

        Cache policy:
        - Keyed on the last bar timestamp of df_15m
        - TTL: 15 minutes (hard cap)
        - Returns HOLD vote when df_15m is None or too short
        """
        if df_15m is None or len(df_15m) < self._mtf_bars_required:
            return Vote(direction="HOLD", strength=0.0, reason="15m MTF no data")

        # Resolve last bar timestamp
        last_idx = df_15m.index[-1]
        if hasattr(last_idx, "timestamp"):
            bar_ts = last_idx.timestamp()
        else:
            bar_ts = float(last_idx)

        now = time.monotonic()
        cached = self._mtf_cache
        if (
            cached.get("bar_ts") == bar_ts
            and (now - cached.get("cached_at", 0.0)) < _MTF_CACHE_TTL
        ):
            return cached["vote"]

        vote = self._compute_mtf_vote(df_15m)
        self._mtf_cache = {"vote": vote, "bar_ts": bar_ts, "cached_at": now}
        logger.debug("SwingStrategy MTF vote refreshed | bar_ts={} | {}", bar_ts, vote)
        return vote

    # ------------------------------------------------------------------
    # Main signal generation
    # ------------------------------------------------------------------

    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
        df_15m: pd.DataFrame | None = None,
    ) -> SwingTradeSignal:
        """Generate swing trading signal from OHLCV DataFrame.

        Args:
            df: OHLCV DataFrame with 4h candles (min 200 bars recommended).
            symbol: Trading symbol.
            df_15m: Optional 15m DataFrame for P1-8 MTF confirmation.

        Returns:
            SwingTradeSignal with signal direction and metadata.
        """
        min_bars = self.ema_slow_period + 10
        if len(df) < min_bars:
            logger.warning(
                "SwingStrategy: insufficient bars {} < {} for {}",
                len(df), min_bars, symbol,
            )
            return SwingTradeSignal(
                signal=SwingSignal.HOLD,
                confidence=0.0,
                entry_price=float(df["close"].iloc[-1]),
                symbol=symbol,
                ema_fast=0.0,
                ema_slow=0.0,
                rsi=50.0,
                support=0.0,
                resistance=0.0,
                reason="insufficient_bars",
                atr=0.0,
            )

        # --- 4h indicators ---
        close = df["close"]
        ema_fast = close.ewm(span=self.ema_fast_period, adjust=False).mean()
        ema_slow = close.ewm(span=self.ema_slow_period, adjust=False).mean()

        rsi_series = ta.rsi(close, length=self.rsi_period)
        atr_series = ta.atr(df["high"], df["low"], close, length=14)

        current_price = float(close.iloc[-1])
        ema_fast_val = float(ema_fast.iloc[-1])
        ema_slow_val = float(ema_slow.iloc[-1])
        rsi_val = float(rsi_series.iloc[-1]) if rsi_series is not None and len(rsi_series) > 0 else 50.0
        atr_val = float(atr_series.iloc[-1]) if atr_series is not None and len(atr_series) > 0 else 0.0

        vol_avg = float(df["volume"].tail(20).mean())
        vol_current = float(df["volume"].iloc[-1])
        volume_ok = vol_current > vol_avg

        support, resistance = self.find_support_resistance(df)

        # P1-8: 15m MTF vote (cached)
        mtf_vote = self.get_mtf_vote(df_15m)

        # --- Signal logic ---
        signal = SwingSignal.HOLD
        confidence = 0.0
        reason = "no_signal"

        golden_cross = ema_fast_val > ema_slow_val
        death_cross = ema_fast_val < ema_slow_val

        rsi_long_zone = self.rsi_long_low <= rsi_val <= self.rsi_long_high
        rsi_short_zone = self.rsi_short_low <= rsi_val <= self.rsi_short_high

        near_support = current_price <= support * 1.02
        near_resistance = current_price >= resistance * 0.98

        if golden_cross and rsi_long_zone and volume_ok:
            signal = SwingSignal.BUY
            confidence = 0.6
            reason = "golden_cross+rsi_long_zone+volume"
            if near_support:
                confidence += 0.15
                reason += "+near_support"
            if mtf_vote.direction == "BUY":
                confidence += 0.1
                reason += "+mtf_bull"
            elif mtf_vote.direction == "SELL":
                confidence -= 0.15
                reason += "+mtf_bear_penalty"

        elif death_cross and rsi_short_zone and volume_ok:
            signal = SwingSignal.SELL
            confidence = 0.6
            reason = "death_cross+rsi_short_zone+volume"
            if near_resistance:
                confidence += 0.15
                reason += "+near_resistance"
            if mtf_vote.direction == "SELL":
                confidence += 0.1
                reason += "+mtf_bear"
            elif mtf_vote.direction == "BUY":
                confidence -= 0.15
                reason += "+mtf_bull_penalty"

        confidence = max(0.0, min(1.0, confidence))

        logger.debug(
            "SwingStrategy {} | {} | conf={:.2f} | EMA {:.4f}/{:.4f} | RSI {:.1f} | "
            "S/R {:.4f}/{:.4f} | vol_ok={} | mtf={}",
            symbol, signal.value, confidence,
            ema_fast_val, ema_slow_val, rsi_val,
            support, resistance, volume_ok, mtf_vote.direction,
        )

        return SwingTradeSignal(
            signal=signal,
            confidence=confidence,
            entry_price=current_price,
            symbol=symbol,
            ema_fast=ema_fast_val,
            ema_slow=ema_slow_val,
            rsi=rsi_val,
            support=support,
            resistance=resistance,
            reason=reason,
            atr=atr_val,
            mtf_vote=mtf_vote,
        )
