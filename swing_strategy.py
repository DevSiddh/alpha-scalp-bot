"""Alpha-Scalp Bot – Swing Trading Strategy Module.

Higher-timeframe strategy for 4h candles:
1. EMA 50/200 golden/death cross detection
2. RSI zone filter (40-50 for longs, 50-60 for shorts)
3. Support/Resistance from recent swing highs/lows (pivot window=5)
4. Volume confirmation
5. ATR calculation for dynamic stop-loss sizing

Designed to catch multi-day moves with wider SL/TP.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd
import pandas_ta as ta
from loguru import logger

import config as cfg


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

        # P1-8: 15m MTF cache
        self._mtf_cache: dict = {}  # {"vote": int, "ts": float, "bar_ts": float}
        self.mtf_ema_fast: int = 9
        self.mtf_ema_slow: int = 21
        self.mtf_rsi_period: int = 14

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

    @staticmethod
    def find_support_resistance(
        df: pd.DataFrame, lookback: int = 50
    ) -> tuple[float, float]:
        """Find support/resistance from recent swing highs/lows.
        
        Uses rolling window to find local minima (support) and maxima (resistance).
        """
        if len(df) < lookback:
            lookback = len(df)

        recent = df.tail(lookback)
        highs = recent["high"].values
        lows = recent["low"].values

        # Find swing highs: points higher than both neighbors
        swing_highs = []
        swing_lows = []
        pivot_window = 5  # look 5 bars each side

        for i in range(pivot_window, len(highs) - pivot_window):
            if highs[i] == max(highs[i - pivot_window : i + pivot_window + 1]):
                swing_highs.append(highs[i])
            if lows[i] == min(lows[i - pivot_window : i + pivot_window + 1]):
                swing_lows.append(lows[i])

        # Use most recent swing levels, fallback to simple min/max
        if swing_highs:
            resistance = np.mean(sorted(swing_highs)[-3:])  # avg top 3
        else:
            resistance = float(recent["high"].max())

        if swing_lows:
            support = np.mean(sorted(swing_lows)[:3])  # avg bottom 3
        else:
            support = float(recent["low"].min())

        return support, resistance

    def calculate_signals(self, df: pd.DataFrame, symbol: str) -> SwingTradeSignal:
        """Analyse 4h OHLCV DataFrame and return a SwingTradeSignal.

        Signal logic:
        BUY  – (a) Golden cross + volume confirm (conf 0.85)
               (b) Pullback to EMA50 in uptrend + RSI 40-50 (conf 0.75)
               (c) Same as (b) + volume + near support (conf 0.80)
        SELL – (a) Death cross + volume confirm (conf 0.85)
               (b) Rally to EMA50 in downtrend + RSI 50-60 (conf 0.75)
               (c) Same as (b) + volume + near resistance (conf 0.80)
        """
        if df is None or len(df) < self.ema_slow_period + 2:
            logger.warning("Insufficient data for swing signal on {}", symbol)
            return SwingTradeSignal(
                signal=SwingSignal.HOLD, confidence=0.0, entry_price=0.0,
                symbol=symbol, ema_fast=0.0, ema_slow=0.0, rsi=0.0,
                support=0.0, resistance=0.0, reason="Insufficient data",
                atr=0.0,
            )

        close = df["close"].astype(float)
        volume = df["volume"].astype(float)

        # Indicators
        ema_fast = ta.ema(close, length=self.ema_fast_period)
        ema_slow = ta.ema(close, length=self.ema_slow_period)
        rsi = ta.rsi(close, length=self.rsi_period)

        # ATR for dynamic stop-loss
        atr_series = ta.atr(df["high"], df["low"], df["close"], length=cfg.SWING_SL_ATR_PERIOD)
        curr_atr = float(atr_series.iloc[-1]) if atr_series is not None and not pd.isna(atr_series.iloc[-1]) else 0.0

        # Support / Resistance
        support, resistance = self.find_support_resistance(df)

        # Volume confirmation: current vol > 1.2x 20-period avg
        vol_avg = volume.rolling(20).mean()
        vol_confirmed = float(volume.iloc[-1]) > float(vol_avg.iloc[-1]) * 1.2 if not pd.isna(vol_avg.iloc[-1]) else False

        # Latest values
        curr_close = float(close.iloc[-1])
        curr_ema_fast = float(ema_fast.iloc[-1]) if ema_fast is not None and not pd.isna(ema_fast.iloc[-1]) else 0.0
        curr_ema_slow = float(ema_slow.iloc[-1]) if ema_slow is not None and not pd.isna(ema_slow.iloc[-1]) else 0.0
        curr_rsi = float(rsi.iloc[-1]) if rsi is not None and not pd.isna(rsi.iloc[-1]) else 50.0

        # EMA cross detection
        if len(ema_fast) >= 2 and len(ema_slow) >= 2:
            prev_fast = float(ema_fast.iloc[-2]) if not pd.isna(ema_fast.iloc[-2]) else 0.0
            prev_slow = float(ema_slow.iloc[-2]) if not pd.isna(ema_slow.iloc[-2]) else 0.0
            curr_f = curr_ema_fast
            curr_s = curr_ema_slow

            golden_cross = (prev_fast <= prev_slow) and (curr_f > curr_s)
            death_cross = (prev_fast >= prev_slow) and (curr_f < curr_s)
        else:
            golden_cross = False
            death_cross = False

        # Trend bias: EMA fast above slow = bullish
        bullish_trend = curr_ema_fast > curr_ema_slow
        bearish_trend = curr_ema_fast < curr_ema_slow

        # Pullback to EMA50: price within 1% of EMA50
        price_near_ema50 = abs(curr_close - curr_ema_fast) / curr_ema_fast < 0.01 if curr_ema_fast > 0 else False

        # RSI zone checks
        rsi_in_long_zone = self.rsi_long_low <= curr_rsi <= self.rsi_long_high  # 40-50
        rsi_in_short_zone = self.rsi_short_low <= curr_rsi <= self.rsi_short_high  # 50-60

        # Proximity to S/R levels (within 1.5% of level)
        sr_range = resistance - support if resistance > support else 1.0
        near_support = (curr_close - support) / sr_range < 0.15
        near_resistance = (resistance - curr_close) / sr_range < 0.15

        # Signal decision
        signal = SwingSignal.HOLD
        confidence = 0.0
        reasons: list[str] = []

        # === BUY SIGNALS ===
        # (a) Golden cross + volume confirm = 0.85
        if golden_cross and vol_confirmed:
            signal = SwingSignal.BUY
            confidence = 0.85
            reasons = [
                f"GOLDEN CROSS: EMA{self.ema_fast_period} crossed above EMA{self.ema_slow_period}",
                f"Volume confirmed ({volume.iloc[-1]:.0f} > avg)",
            ]
        # (c) Pullback to EMA50 in uptrend + RSI 40-50 + volume + near support = 0.80
        elif bullish_trend and price_near_ema50 and rsi_in_long_zone and vol_confirmed and near_support:
            signal = SwingSignal.BUY
            confidence = 0.80
            reasons = [
                f"Pullback to EMA{self.ema_fast_period} in uptrend",
                f"RSI={curr_rsi:.1f} in zone [{self.rsi_long_low}-{self.rsi_long_high}]",
                f"Near support {support:.2f}",
                f"Volume confirmed",
            ]
        # (b) Pullback to EMA50 in uptrend + RSI 40-50 = 0.75
        elif bullish_trend and price_near_ema50 and rsi_in_long_zone:
            signal = SwingSignal.BUY
            confidence = 0.75
            reasons = [
                f"Pullback to EMA{self.ema_fast_period} in uptrend",
                f"RSI={curr_rsi:.1f} in zone [{self.rsi_long_low}-{self.rsi_long_high}]",
            ]

        # === SELL SIGNALS ===
        # (a) Death cross + volume confirm = 0.85
        elif death_cross and vol_confirmed:
            signal = SwingSignal.SELL
            confidence = 0.85
            reasons = [
                f"DEATH CROSS: EMA{self.ema_fast_period} crossed below EMA{self.ema_slow_period}",
                f"Volume confirmed ({volume.iloc[-1]:.0f} > avg)",
            ]
        # (c) Rally to EMA50 in downtrend + RSI 50-60 + volume + near resistance = 0.80
        elif bearish_trend and price_near_ema50 and rsi_in_short_zone and vol_confirmed and near_resistance:
            signal = SwingSignal.SELL
            confidence = 0.80
            reasons = [
                f"Rally to EMA{self.ema_fast_period} in downtrend",
                f"RSI={curr_rsi:.1f} in zone [{self.rsi_short_low}-{self.rsi_short_high}]",
                f"Near resistance {resistance:.2f}",
                f"Volume confirmed",
            ]
        # (b) Rally to EMA50 in downtrend + RSI 50-60 = 0.75
        elif bearish_trend and price_near_ema50 and rsi_in_short_zone:
            signal = SwingSignal.SELL
            confidence = 0.75
            reasons = [
                f"Rally to EMA{self.ema_fast_period} in downtrend",
                f"RSI={curr_rsi:.1f} in zone [{self.rsi_short_low}-{self.rsi_short_high}]",
            ]
        else:
            reasons = ["No swing opportunity \u2013 HOLD"]

        reason_str = " | ".join(reasons)

        trade_signal = SwingTradeSignal(
            signal=signal,
            confidence=round(confidence, 3),
            entry_price=curr_close,
            symbol=symbol,
            ema_fast=round(curr_ema_fast, 2),
            ema_slow=round(curr_ema_slow, 2),
            rsi=round(curr_rsi, 2),
            support=round(support, 2),
            resistance=round(resistance, 2),
            reason=reason_str,
            atr=round(curr_atr, 4),
        )

        if signal != SwingSignal.HOLD:
            logger.info(
                "[SWING] {} SIGNAL on {} | conf={:.1%} | ATR={:.4f} | {}",
                signal.value, symbol, confidence, curr_atr, reason_str,
            )
        else:
            logger.debug(
                "[SWING] HOLD on {} | EMA={:.2f}/{:.2f} RSI={:.1f} ATR={:.4f} S/R={:.2f}/{:.2f}",
                symbol, curr_ema_fast, curr_ema_slow, curr_rsi, curr_atr, support, resistance,
            )

        return trade_signal

    def get_mtf_bias(self, exchange) -> int:
        """Fetch 15m klines and return MTF bias vote: +1 BUY, -1 SELL, 0 HOLD.

        Caches result for 15 minutes (900s). Uses same REST method as 4h klines.
        """
        import time
        now = time.time()
        cache_ttl = 900  # 15 minutes

        # Refresh on new 15m candle or cache expiry
        if self._mtf_cache and (now - self._mtf_cache.get("ts", 0)) < cache_ttl:
            return self._mtf_cache.get("vote", 0)

        try:
            symbol = cfg.SYMBOL
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=50)
            if not ohlcv or len(ohlcv) < self.mtf_ema_slow + 2:
                return 0

            df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
            df["ema_fast"] = ta.ema(df["close"], length=self.mtf_ema_fast)
            df["ema_slow"] = ta.ema(df["close"], length=self.mtf_ema_slow)
            df["rsi"] = ta.rsi(df["close"], length=self.mtf_rsi_period)

            last = df.iloc[-1]
            ema_fast = last["ema_fast"]
            ema_slow = last["ema_slow"]
            rsi = last["rsi"]

            if pd.isna(ema_fast) or pd.isna(ema_slow) or pd.isna(rsi):
                return 0

            if ema_fast > ema_slow and rsi > 45:
                vote = 1   # BUY strength 0.7 mapped to +1
            elif ema_fast < ema_slow and rsi < 55:
                vote = -1  # SELL strength 0.7 mapped to -1
            else:
                vote = 0

            self._mtf_cache = {"vote": vote, "ts": now}
            logger.debug("mtf_bias(15m) | ema_fast={:.2f} ema_slow={:.2f} rsi={:.1f} vote={}",
                        ema_fast, ema_slow, rsi, vote)
            return vote

        except Exception as exc:
            logger.warning("mtf_bias fetch failed: {} — using 0", exc)
            return self._mtf_cache.get("vote", 0)

