"""Alpha-Scalp Bot – Premium Trading Strategy Module.

Signal generation pipeline:
1. EMA 9/21 crossover detection
2. RSI 14 momentum filter
3. Nadaraya-Watson Gaussian kernel envelope for mean-reversion context
4. Volume spike confirmation (>1.5x 20-period SMA)
5. Bollinger Band squeeze breakout detection
6. ADX regime detection (trending vs ranging)
7. Kelly Criterion dynamic position sizing

A trade signal fires only when ALL core conditions align,
with premium filters boosting confidence and reducing false signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import numpy as np
import pandas as pd
import pandas_ta as ta
from loguru import logger

import config as cfg


# ---------------------------------------------------------------------------
# P2-11: Session filter + funding settlement block
# ---------------------------------------------------------------------------

DEAD_ZONES_UTC: list[tuple[int, int, int, int]] = [(21, 0, 23, 59), (0, 0, 5, 30)]

ACTIVE_SESSIONS_UTC: list[tuple[int, int, int, int]] = [
    (8, 0, 10, 30),
    (13, 30, 16, 30),
    (14, 30, 17, 30),
]

FUNDING_SETTLEMENT_UTC_HOURS: list[int] = [0, 8, 16]


def _in_time_range(h: int, m: int, sh: int, sm: int, eh: int, em: int) -> bool:
    t = h * 60 + m
    return (sh * 60 + sm) <= t <= (eh * 60 + em)


def check_session_filter(dt_utc: "datetime") -> tuple[bool, float]:
    import os
    if not os.getenv("IS_SESSION_FILTER_ENABLED", "true").lower() in ("1", "true", "yes"):
        return True, 1.0

    h, m = dt_utc.hour, dt_utc.minute

    for sh, sm, eh, em in DEAD_ZONES_UTC:
        if _in_time_range(h, m, sh, sm, eh, em):
            logger.debug("Session filter: DEAD ZONE {:02d}:{:02d} UTC — entry blocked", h, m)
            return False, 0.0

    if h in FUNDING_SETTLEMENT_UTC_HOURS and m >= 50:
        logger.debug(
            "Session filter: FUNDING SETTLEMENT {:02d}:{:02d} UTC — entry blocked", h, m
        )
        return False, 0.0

    in_active = any(
        _in_time_range(h, m, sh, sm, eh, em)
        for sh, sm, eh, em in ACTIVE_SESSIONS_UTC
    )
    conf_mult = 1.0 if in_active else 0.8
    if not in_active:
        logger.debug("Session filter: outside active session {:02d}:{:02d} UTC — conf x0.8", h, m)

    return True, conf_mult


# ---------------------------------------------------------------------------
# Signal types
# ---------------------------------------------------------------------------
class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"


@dataclass
class TradeSignal:
    signal: Signal
    confidence: float
    entry_price: float
    ema_fast: float
    ema_slow: float
    rsi: float
    nw_mid: float
    nw_upper: float
    nw_lower: float
    reason: str
    atr: float = 0.0
    regime: MarketRegime = MarketRegime.RANGING
    adx: float = 0.0
    volume_ratio: float = 0.0
    bb_squeeze: bool = False
    kelly_fraction: float = 0.0


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
class ScalpStrategy:
    """Premium RSI + EMA + NW + Volume + Bollinger + ADX Regime strategy."""

    def __init__(self) -> None:
        self.ema_fast_period: int = cfg.EMA_FAST
        self.ema_slow_period: int = cfg.EMA_SLOW

        self.rsi_period: int = cfg.RSI_PERIOD
        self.rsi_oversold: int = cfg.RSI_OVERSOLD
        self.rsi_overbought: int = cfg.RSI_OVERBOUGHT

        self.nw_bandwidth: float = cfg.NW_BANDWIDTH
        self.nw_mult: float = cfg.NW_MULT
        self.nw_lookback: int = cfg.NW_LOOKBACK

        self.vol_sma_period: int = getattr(cfg, 'VOL_SMA_PERIOD', 20)
        self.vol_spike_mult: float = getattr(cfg, 'VOL_SPIKE_MULT', 1.5)

        self.bb_period: int = getattr(cfg, 'BB_PERIOD', 20)
        self.bb_std: float = getattr(cfg, 'BB_STD', 2.0)
        self.bb_squeeze_threshold: float = getattr(cfg, 'BB_SQUEEZE_THRESHOLD', 0.02)

        self.adx_period: int = getattr(cfg, 'ADX_PERIOD', 14)
        self.adx_trend_threshold: float = getattr(cfg, 'ADX_TREND_THRESHOLD', 25.0)
        self.adx_strong_trend: float = getattr(cfg, 'ADX_STRONG_TREND', 40.0)

        self._win_count: int = 0
        self._loss_count: int = 0
        self._total_wins_r: float = 0.0
        self._total_losses_r: float = 0.0

        logger.info(
            "ScalpStrategy PREMIUM initialised | EMA {}/{} | RSI {} ({}/<{}) | "
            "NW bw={} mult={} lb={} | Vol SMA={} spike={}x | "
            "BB {}/{:.1f} squeeze={:.2%} | ADX {} trend>{} strong>{}",
            self.ema_fast_period, self.ema_slow_period,
            self.rsi_period, self.rsi_oversold, self.rsi_overbought,
            self.nw_bandwidth, self.nw_mult, self.nw_lookback,
            self.vol_sma_period, self.vol_spike_mult,
            self.bb_period, self.bb_std, self.bb_squeeze_threshold,
            self.adx_period, self.adx_trend_threshold, self.adx_strong_trend,
        )

    @staticmethod
    def nadaraya_watson_envelope(
        close_prices: np.ndarray,
        bandwidth: float,
        mult: float,
        lookback: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = len(close_prices)
        nw_mid = np.full(n, np.nan)
        nw_upper = np.full(n, np.nan)
        nw_lower = np.full(n, np.nan)

        if n < lookback:
            logger.warning("Not enough data for NW envelope ({} < {})", n, lookback)
            return nw_mid, nw_upper, nw_lower

        for i in range(lookback - 1, n):
            start = i - lookback + 1
            window = close_prices[start : i + 1]
            m = len(window)

            distances = np.arange(m - 1, -1, -1, dtype=np.float64)
            weights = np.exp(-(distances ** 2) / (2 * bandwidth ** 2))

            weight_sum = weights.sum()
            if weight_sum == 0:
                continue

            y_hat = np.dot(weights, window) / weight_sum
            nw_mid[i] = y_hat

            residuals = window - y_hat
            weighted_var = np.dot(weights, residuals ** 2) / weight_sum
            std = np.sqrt(weighted_var)

            nw_upper[i] = y_hat + mult * std
            nw_lower[i] = y_hat - mult * std

        return nw_mid, nw_upper, nw_lower

    @staticmethod
    def _detect_cross(fast: pd.Series, slow: pd.Series) -> tuple[bool, bool]:
        if len(fast) < 2 or len(slow) < 2:
            return False, False

        prev_fast, curr_fast = fast.iloc[-2], fast.iloc[-1]
        prev_slow, curr_slow = slow.iloc[-2], slow.iloc[-1]

        cross_above = (prev_fast <= prev_slow) and (curr_fast > curr_slow)
        cross_below = (prev_fast >= prev_slow) and (curr_fast < curr_slow)
        return cross_above, cross_below

    def _check_volume_spike(self, volume: pd.Series) -> tuple[bool, float]:
        if len(volume) < self.vol_sma_period + 1:
            return False, 1.0

        vol_sma = volume.rolling(self.vol_sma_period).mean()
        curr_vol = float(volume.iloc[-1])
        avg_vol = float(vol_sma.iloc[-1])

        if avg_vol <= 0:
            return False, 1.0

        ratio = curr_vol / avg_vol
        is_spike = ratio >= self.vol_spike_mult

        if is_spike:
            logger.debug("Volume SPIKE: {:.2f}x avg (cur={:.0f}, avg={:.0f})",
                         ratio, curr_vol, avg_vol)
        return is_spike, round(ratio, 2)

    def _check_bb_squeeze(self, close: pd.Series) -> tuple[bool, float, float, float]:
        if len(close) < self.bb_period + 1:
            return False, 0.0, 0.0, 0.0

        bb = ta.bbands(close, length=self.bb_period, std=self.bb_std)
        if bb is None or bb.empty:
            return False, 0.0, 0.0, 0.0

        bb_upper_col = f"BBU_{self.bb_period}_{self.bb_std}"
        bb_lower_col = f"BBL_{self.bb_period}_{self.bb_std}"
        bb_mid_col = f"BBM_{self.bb_period}_{self.bb_std}"

        if bb_upper_col not in bb.columns:
            return False, 0.0, 0.0, 0.0

        bb_upper = float(bb[bb_upper_col].iloc[-1])
        bb_lower = float(bb[bb_lower_col].iloc[-1])
        bb_mid = float(bb[bb_mid_col].iloc[-1])

        if bb_mid <= 0:
            return False, bb_upper, bb_lower, 0.0

        bb_width_pct = (bb_upper - bb_lower) / bb_mid
        is_squeeze = bb_width_pct < self.bb_squeeze_threshold

        if is_squeeze:
            logger.debug("BB SQUEEZE detected: width={:.4%} < {:.4%}",
                         bb_width_pct, self.bb_squeeze_threshold)
        return is_squeeze, bb_upper, bb_lower, round(bb_width_pct, 4)

    def _detect_regime(self, high: pd.Series, low: pd.Series, close: pd.Series
    ) -> tuple[MarketRegime, float]:
        adx = ta.adx(high, low, close, length=self.adx_period)
        if adx is None or adx.empty:
            return MarketRegime.RANGING, 0.0

        adx_col = f"ADX_{self.adx_period}"
        if adx_col not in adx.columns:
            return MarketRegime.RANGING, 0.0

        curr_adx = float(adx[adx_col].iloc[-1])
        if pd.isna(curr_adx):
            return MarketRegime.RANGING, 0.0

        if curr_adx >= self.adx_strong_trend:
            regime = MarketRegime.VOLATILE
        elif curr_adx >= self.adx_trend_threshold:
            regime = MarketRegime.TRENDING
        else:
            regime = MarketRegime.RANGING

        logger.debug("Market regime: {} (ADX={:.1f})", regime.value, curr_adx)
        return regime, round(curr_adx, 1)

    def update_kelly_stats(self, won: bool, reward_risk_ratio: float) -> None:
        """Update win/loss stats for Kelly calculation. Call after each trade."""
        if won:
            self._win_count += 1
            self._total_wins_r += reward_risk_ratio
        else:
            self._loss_count += 1
            self._total_losses_r += reward_risk_ratio

    def get_kelly_fraction(self) -> float:
        total = self._win_count + self._loss_count
        if total < 10:
            return cfg.RISK_PER_TRADE

        win_rate = self._win_count / total
        avg_rr = self._total_wins_r / max(self._win_count, 1)

        if avg_rr <= 0:
            return cfg.RISK_PER_TRADE

        kelly = win_rate - ((1 - win_rate) / avg_rr)
        kelly = kelly * 0.25
        kelly = max(0.005, min(kelly, 0.05))

        logger.debug(
            "Kelly: {:.2%} (W={:.1%}, R={:.2f}, trades={})",
            kelly, win_rate, avg_rr, total,
        )
        return round(kelly, 4)

    def calculate_signals(self, df: pd.DataFrame) -> TradeSignal:
        if df is None or len(df) < self.ema_slow_period + 2:
            logger.warning("Insufficient data for signal calculation")
            return TradeSignal(
                signal=Signal.HOLD, confidence=0.0, entry_price=0.0,
                ema_fast=0.0, ema_slow=0.0, rsi=0.0,
                nw_mid=0.0, nw_upper=0.0, nw_lower=0.0,
                reason="Insufficient data",
            )

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        ema_fast = ta.ema(close, length=self.ema_fast_period)
        ema_slow = ta.ema(close, length=self.ema_slow_period)
        rsi = ta.rsi(close, length=self.rsi_period)

        df["atr"] = ta.atr(high, low, close, length=cfg.SCALP_SL_ATR_PERIOD)

        atr_series = ta.atr(high, low, close, length=14)
        curr_atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 100.0
        dynamic_mult = max(0.5, min(2.5, curr_atr_val / 200.0))
        effective_mult = self.nw_mult * dynamic_mult

        nw_mid, nw_upper, nw_lower = self.nadaraya_watson_envelope(
            close.values, self.nw_bandwidth, effective_mult, self.nw_lookback
        )

        vol_spike, vol_ratio = self._check_volume_spike(volume)
        bb_squeeze, bb_upper_val, bb_lower_val, bb_width = self._check_bb_squeeze(close)
        regime, adx_val = self._detect_regime(high, low, close)
        kelly = self.get_kelly_fraction()

        curr_close = float(close.iloc[-1])
        curr_ema_fast = float(ema_fast.iloc[-1]) if ema_fast is not None else 0.0
        curr_ema_slow = float(ema_slow.iloc[-1]) if ema_slow is not None else 0.0
        curr_rsi = float(rsi.iloc[-1]) if rsi is not None else 50.0
        curr_nw_mid = float(nw_mid[-1]) if not np.isnan(nw_mid[-1]) else curr_close
        curr_nw_upper = float(nw_upper[-1]) if not np.isnan(nw_upper[-1]) else curr_close * 1.01
        curr_nw_lower = float(nw_lower[-1]) if not np.isnan(nw_lower[-1]) else curr_close * 0.99

        cross_above, cross_below = self._detect_cross(ema_fast, ema_slow)

        # NW band crossover
        prev_close = float(close.iloc[-2])
        long_cross  = (prev_close < curr_nw_lower) and (curr_close > curr_nw_lower)
        short_cross = (prev_close < curr_nw_upper) and (curr_close >= curr_nw_upper)

        signal = Signal.HOLD
        confidence = 0.0
        reasons: list[str] = []

        buy_ema = cross_above
        buy_rsi = curr_rsi < self.rsi_oversold
        buy_nw = long_cross

        sell_ema = cross_below
        sell_rsi = curr_rsi > self.rsi_overbought
        sell_nw = short_cross

        premium_confirm = vol_spike
        premium_boost = bb_squeeze or regime in (MarketRegime.TRENDING, MarketRegime.VOLATILE)

        if buy_ema and buy_rsi and buy_nw and premium_confirm:
            signal = Signal.BUY
            rsi_strength = (self.rsi_oversold - curr_rsi) / self.rsi_oversold
            nw_strength = max(0, (curr_nw_lower - curr_close) / (curr_nw_upper - curr_nw_lower + 1e-9))
            vol_strength = min(1.0, (vol_ratio - 1.0) / 2.0)

            confidence = min(1.0, 0.3 + 0.2 * rsi_strength + 0.2 * nw_strength + 0.15 * vol_strength)
            if premium_boost:
                confidence = min(1.0, confidence + 0.15)

            reasons = [
                f"EMA{self.ema_fast_period} crossed above EMA{self.ema_slow_period}",
                f"RSI={curr_rsi:.1f} < {self.rsi_oversold} (oversold)",
                f"Price {curr_close:.2f} crossed below NW lower {curr_nw_lower:.2f}",
                f"Volume {vol_ratio:.1f}x avg (CONFIRMED)",
            ]
            if bb_squeeze:
                reasons.append(f"BB Squeeze (width={bb_width:.4f}) - breakout imminent")
            if regime != MarketRegime.RANGING:
                reasons.append(f"Regime: {regime.value} (ADX={adx_val:.1f})")

        elif sell_ema and sell_rsi and sell_nw and premium_confirm:
            signal = Signal.SELL
            rsi_strength = (curr_rsi - self.rsi_overbought) / (100 - self.rsi_overbought)
            nw_strength = max(0, (curr_close - curr_nw_upper) / (curr_nw_upper - curr_nw_lower + 1e-9))
            vol_strength = min(1.0, (vol_ratio - 1.0) / 2.0)

            confidence = min(1.0, 0.3 + 0.2 * rsi_strength + 0.2 * nw_strength + 0.15 * vol_strength)
            if premium_boost:
                confidence = min(1.0, confidence + 0.15)

            reasons = [
                f"EMA{self.ema_fast_period} crossed below EMA{self.ema_slow_period}",
                f"RSI={curr_rsi:.1f} > {self.rsi_overbought} (overbought)",
                f"Price {curr_close:.2f} crossed above NW upper {curr_nw_upper:.2f}",
                f"Volume {vol_ratio:.1f}x avg (CONFIRMED)",
            ]
            if bb_squeeze:
                reasons.append(f"BB Squeeze (width={bb_width:.4f}) - breakout imminent")
            if regime != MarketRegime.RANGING:
                reasons.append(f"Regime: {regime.value} (ADX={adx_val:.1f})")

        elif buy_ema and buy_rsi and buy_nw and not premium_confirm:
            reasons = [f"Near-miss BUY: volume {vol_ratio:.1f}x < {self.vol_spike_mult}x required"]
            logger.info("FILTERED: BUY signal blocked by volume filter ({}x < {}x)",
                        vol_ratio, self.vol_spike_mult)
        elif sell_ema and sell_rsi and sell_nw and not premium_confirm:
            reasons = [f"Near-miss SELL: volume {vol_ratio:.1f}x < {self.vol_spike_mult}x required"]
            logger.info("FILTERED: SELL signal blocked by volume filter ({}x < {}x)",
                        vol_ratio, self.vol_spike_mult)
        else:
            reasons = ["No confluence – HOLD"]

        curr_atr = float(df["atr"].iloc[-1]) if not pd.isna(df["atr"].iloc[-1]) else 0.0

        if signal != Signal.HOLD:
            _now_utc = datetime.now(timezone.utc)
            _allow, _conf_mult = check_session_filter(_now_utc)
            if not _allow:
                logger.info(
                    "SESSION FILTER: {} blocked at {:02d}:{:02d} UTC",
                    signal.value, _now_utc.hour, _now_utc.minute,
                )
                signal = Signal.HOLD
                confidence = 0.0
                reasons = [f"Session filter blocked entry at {_now_utc.strftime('%H:%M')} UTC"]
            elif _conf_mult < 1.0:
                confidence = round(confidence * _conf_mult, 3)
                reasons.append(f"Session filter: confidence reduced to {_conf_mult:.0%} (outside active session)")
                logger.debug("Session filter: confidence reduced by {:.0%}", _conf_mult)

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
            atr=round(curr_atr, 2),
            regime=regime,
            adx=adx_val,
            volume_ratio=vol_ratio,
            bb_squeeze=bb_squeeze,
            kelly_fraction=kelly,
        )

        if signal != Signal.HOLD:
            logger.info(
                "SIGNAL {} | conf={:.1%} | regime={} | ADX={:.1f} | vol={}x | kelly={:.2%} | {}",
                signal.value, confidence, regime.value, adx_val, vol_ratio, kelly, reason_str,
            )
        else:
            logger.debug(
                "HOLD | EMA_up={} EMA_dn={} RSI={:.1f} vol={}x regime={} "
                "close={:.2f} NW=[{:.2f}, {:.2f}]",
                cross_above, cross_below, curr_rsi, vol_ratio,
                regime.value, curr_close, curr_nw_lower, curr_nw_upper,
            )

        return trade_signal


# Alias for backward compatibility
Strategy = ScalpStrategy
