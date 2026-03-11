"""Alpha-Scalp Bot – Feature Cache Module (Phase 1).

Compute all technical indicators ONCE per candle update, store in a
dictionary, and let every downstream engine (AlphaEngine, SignalScoring,
RiskEngine) read from the cache.  Prevents duplicate calculations and
eliminates indicator drift between modules.

Usage:
    cache = FeatureCache()
    features = cache.compute(df)          # df = OHLCV DataFrame
    ema_trend = features.ema_trend        # +1 fast>slow, -1 fast<slow
    rsi       = features.rsi
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import pandas_ta as ta
from loguru import logger

import config as cfg


@dataclass
class FeatureSet:
    """Immutable snapshot of all computed features for the latest bar."""

    # --- Price ---
    close: float = 0.0
    prev_close: float = 0.0
    high: float = 0.0
    low: float = 0.0

    # --- EMA ---
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_trend: int = 0           # +1 fast > slow, -1 fast < slow, 0 flat
    ema_cross_up: bool = False
    ema_cross_down: bool = False

    # --- RSI ---
    rsi: float = 50.0

    # --- ATR ---
    atr: float = 0.0

    # --- Nadaraya-Watson Envelope ---
    nw_mid: float = 0.0
    nw_upper: float = 0.0
    nw_lower: float = 0.0
    nw_long_cross: bool = False   # price crossed below lower band
    nw_short_cross: bool = False  # price crossed above upper band

    # --- Volume ---
    volume_ratio: float = 1.0    # current_vol / sma_vol
    volume_spike: bool = False

    # --- Bollinger Bands ---
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_width_pct: float = 0.0
    bb_squeeze: bool = False

    # --- ADX / Regime ---
    adx: float = 0.0
    regime: str = "RANGING"       # TRENDING | RANGING | VOLATILE

    # --- VWAP ---
    vwap: float = 0.0

    # --- CVD (Cumulative Volume Delta) ---
    cvd_raw: float = 0.0         # raw CVD value (latest bar)
    cvd_slope: float = 0.0       # normalised slope over lookback (-1 to +1)
    cvd_divergence: int = 0      # +1 bullish div (price down, CVD up), -1 bearish, 0 none

    def as_dict(self) -> dict[str, Any]:
        """Return all features as a flat dictionary."""
        return {
            k: getattr(self, k)
            for k in self.__dataclass_fields__
        }


class FeatureCache:
    """Compute indicators once, read everywhere.

    Call ``compute(df)`` each loop iteration.  All engines then read
    from the returned FeatureSet instead of recalculating.
    """

    def __init__(self) -> None:
        self._last_features: FeatureSet | None = None
        logger.info("FeatureCache initialised")

    def compute(self, df: pd.DataFrame) -> FeatureSet:
        """Compute all indicators from an OHLCV DataFrame.

        Parameters
        ----------
        df : pd.DataFrame
            Must contain columns: open, high, low, close, volume.

        Returns
        -------
        FeatureSet
            Snapshot of every indicator for the latest bar.
        """
        if df is None or len(df) < cfg.EMA_SLOW + 5:
            logger.warning("FeatureCache: insufficient data ({} rows)", len(df) if df is not None else 0)
            return FeatureSet()

        # CPU optimisation: only keep last 150 rows for TA calculations.
        # RSI(14) needs ~28 rows, EMA(50) needs ~100, ADX(14) needs ~42.
        # 150 gives ample buffer for all indicators without recalculating
        # the full history on every candle.
        MAX_ROWS = 150
        if len(df) > MAX_ROWS:
            df = df.tail(MAX_ROWS).reset_index(drop=True)

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        fs = FeatureSet()

        # --- Price --------------------------------------------------------
        fs.close = float(close.iloc[-1])
        fs.prev_close = float(close.iloc[-2])
        fs.high = float(high.iloc[-1])
        fs.low = float(low.iloc[-1])

        # --- EMA ----------------------------------------------------------
        ema_f = ta.ema(close, length=cfg.EMA_FAST)
        ema_s = ta.ema(close, length=cfg.EMA_SLOW)
        fs.ema_fast = float(ema_f.iloc[-1]) if ema_f is not None else 0.0
        fs.ema_slow = float(ema_s.iloc[-1]) if ema_s is not None else 0.0

        if fs.ema_fast > fs.ema_slow:
            fs.ema_trend = 1
        elif fs.ema_fast < fs.ema_slow:
            fs.ema_trend = -1

        # Cross detection
        if ema_f is not None and ema_s is not None and len(ema_f) >= 2:
            prev_f, curr_f = float(ema_f.iloc[-2]), float(ema_f.iloc[-1])
            prev_s, curr_s = float(ema_s.iloc[-2]), float(ema_s.iloc[-1])
            fs.ema_cross_up = (prev_f <= prev_s) and (curr_f > curr_s)
            fs.ema_cross_down = (prev_f >= prev_s) and (curr_f < curr_s)

        # --- RSI ----------------------------------------------------------
        rsi = ta.rsi(close, length=cfg.RSI_PERIOD)
        fs.rsi = float(rsi.iloc[-1]) if rsi is not None and not pd.isna(rsi.iloc[-1]) else 50.0

        # --- ATR ----------------------------------------------------------
        atr = ta.atr(high, low, close, length=cfg.SCALP_SL_ATR_PERIOD)
        fs.atr = float(atr.iloc[-1]) if atr is not None and not pd.isna(atr.iloc[-1]) else 0.0

        # --- Nadaraya-Watson Envelope -------------------------------------
        from strategy import ScalpStrategy
        atr_14 = ta.atr(high, low, close, length=14)
        curr_atr_val = float(atr_14.iloc[-1]) if atr_14 is not None and not pd.isna(atr_14.iloc[-1]) else 100.0
        dynamic_mult = max(0.5, min(2.5, curr_atr_val / 200.0))
        effective_mult = cfg.NW_MULT * dynamic_mult

        nw_mid, nw_upper, nw_lower = ScalpStrategy.nadaraya_watson_envelope(
            close.values, cfg.NW_BANDWIDTH, effective_mult, cfg.NW_LOOKBACK
        )

        fs.nw_mid = float(nw_mid[-1]) if not np.isnan(nw_mid[-1]) else fs.close
        fs.nw_upper = float(nw_upper[-1]) if not np.isnan(nw_upper[-1]) else fs.close * 1.01
        fs.nw_lower = float(nw_lower[-1]) if not np.isnan(nw_lower[-1]) else fs.close * 0.99

        # NW crosses
        fs.nw_long_cross = (fs.prev_close > fs.nw_lower) and (fs.close <= fs.nw_lower)
        fs.nw_short_cross = (fs.prev_close < fs.nw_upper) and (fs.close >= fs.nw_upper)

        # --- Volume -------------------------------------------------------
        if len(volume) >= cfg.VOL_SMA_PERIOD + 1:
            vol_sma = volume.rolling(cfg.VOL_SMA_PERIOD).mean()
            curr_vol = float(volume.iloc[-1])
            avg_vol = float(vol_sma.iloc[-1])
            if avg_vol > 0:
                fs.volume_ratio = round(curr_vol / avg_vol, 2)
                fs.volume_spike = fs.volume_ratio >= cfg.VOL_SPIKE_MULT

        # --- Bollinger Bands ----------------------------------------------
        bb = ta.bbands(close, length=cfg.BB_PERIOD, std=cfg.BB_STD)
        if bb is not None and not bb.empty:
            bb_u_col = f"BBU_{cfg.BB_PERIOD}_{cfg.BB_STD}"
            bb_l_col = f"BBL_{cfg.BB_PERIOD}_{cfg.BB_STD}"
            bb_m_col = f"BBM_{cfg.BB_PERIOD}_{cfg.BB_STD}"
            if bb_u_col in bb.columns:
                fs.bb_upper = float(bb[bb_u_col].iloc[-1])
                fs.bb_lower = float(bb[bb_l_col].iloc[-1])
                bb_mid = float(bb[bb_m_col].iloc[-1])
                if bb_mid > 0:
                    fs.bb_width_pct = round((fs.bb_upper - fs.bb_lower) / bb_mid, 4)
                    fs.bb_squeeze = fs.bb_width_pct < cfg.BB_SQUEEZE_THRESHOLD

        # --- ADX / Regime -------------------------------------------------
        adx_df = ta.adx(high, low, close, length=cfg.ADX_PERIOD)
        if adx_df is not None and not adx_df.empty:
            adx_col = f"ADX_{cfg.ADX_PERIOD}"
            if adx_col in adx_df.columns:
                val = float(adx_df[adx_col].iloc[-1])
                if not pd.isna(val):
                    fs.adx = round(val, 1)
                    if val >= cfg.ADX_STRONG_TREND:
                        fs.regime = "VOLATILE"
                    elif val >= cfg.ADX_TREND_THRESHOLD:
                        fs.regime = "TRENDING"
                    else:
                        fs.regime = "RANGING"

        # --- CVD (Cumulative Volume Delta) --------------------------------
        # Approximation from OHLCV: classify each candle's volume as
        # buy or sell based on close vs open (aggressive side inference).
        # CVD = cumsum(buy_volume - sell_volume)
        if cfg.CVD_ENABLED and len(df) >= cfg.CVD_LOOKBACK + 1:
            try:
                # Delta per bar: proportion of volume that was "buy"
                # Using (close - low) / (high - low) as buy fraction
                # This is more accurate than simple close > open
                bar_range = high - low
                # Avoid division by zero on doji candles
                safe_range = bar_range.replace(0, np.nan).fillna(1e-10)
                buy_fraction = (close - low) / safe_range
                buy_fraction = buy_fraction.clip(0, 1)

                delta = volume * (2 * buy_fraction - 1)  # maps [0,1] -> [-1,+1] * volume
                cvd = delta.cumsum()

                fs.cvd_raw = float(cvd.iloc[-1])

                # Slope: linear regression slope over lookback, normalised
                lookback = cfg.CVD_LOOKBACK
                cvd_window = cvd.iloc[-lookback:].values
                if len(cvd_window) == lookback:
                    x = np.arange(lookback, dtype=float)
                    x_mean = x.mean()
                    cvd_mean = cvd_window.mean()
                    slope = np.sum((x - x_mean) * (cvd_window - cvd_mean)) / (np.sum((x - x_mean) ** 2) + 1e-10)
                    # Normalise by average absolute volume to get a -1 to +1 scale
                    avg_vol = float(volume.iloc[-lookback:].mean())
                    if avg_vol > 0:
                        fs.cvd_slope = float(np.clip(slope / avg_vol, -1.0, 1.0))
                    else:
                        fs.cvd_slope = 0.0

                # Divergence detection: price trending one way, CVD the other
                price_slope_window = close.iloc[-lookback:].values
                if len(price_slope_window) == lookback:
                    x = np.arange(lookback, dtype=float)
                    x_mean = x.mean()
                    p_mean = price_slope_window.mean()
                    price_slope = np.sum((x - x_mean) * (price_slope_window - p_mean)) / (np.sum((x - x_mean) ** 2) + 1e-10)
                    # Bullish divergence: price falling but CVD rising
                    if price_slope < 0 and fs.cvd_slope > cfg.CVD_MILD_THRESHOLD:
                        fs.cvd_divergence = 1
                    # Bearish divergence: price rising but CVD falling
                    elif price_slope > 0 and fs.cvd_slope < -cfg.CVD_MILD_THRESHOLD:
                        fs.cvd_divergence = -1

            except Exception as cvd_exc:
                logger.debug("CVD computation error: {}", cvd_exc)

        # --- VWAP ---------------------------------------------------------
        try:
            typical = (high + low + close) / 3
            cum_tp_vol = (typical * volume).cumsum()
            cum_vol = volume.cumsum()
            vwap_series = cum_tp_vol / cum_vol
            fs.vwap = round(float(vwap_series.iloc[-1]), 2)
        except Exception:
            fs.vwap = fs.close

        self._last_features = fs

        logger.debug(
            "FeatureCache computed | close={:.2f} EMA={}/{} RSI={:.1f} "
            "ADX={:.1f} regime={} vol={}x BB_sq={}",
            fs.close, fs.ema_trend, round(fs.ema_fast - fs.ema_slow, 2),
            fs.rsi, fs.adx, fs.regime, fs.volume_ratio, fs.bb_squeeze,
        )
        return fs

    @property
    def last(self) -> FeatureSet | None:
        """Return the most recently computed feature set."""
        return self._last_features
