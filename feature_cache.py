"""Alpha-Scalp Bot – Feature Cache Module.

Compute all technical indicators ONCE per candle update, store in a
dictionary, and let every downstream engine (AlphaEngine, SignalScoring,
RiskEngine) read from the cache. Prevents duplicate calculations and
eliminates indicator drift between modules.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import time
from collections import deque

import pandas as pd
import numpy as np
import pandas_ta as ta
import config as cfg
from loguru import logger

class OrderFlowCache:
    """Cache for raw order flow and trade streams."""
    def __init__(self):
        self.book_snapshots = deque(maxlen=10)
        self.recent_trades = deque()

    def add_snapshot(self, snapshot: dict) -> None:
        self.book_snapshots.append(snapshot)

    def add_trade(self, trade: dict) -> None:
        # Expect trade dict slightly conforming to: {'timestamp': float, 'qty': float, 'is_buyer_maker': bool}
        # Add trade and clear old ones (>30s)
        current_time = time.time()
        if 'timestamp' not in trade:
            trade['timestamp'] = current_time * 1000
        self.recent_trades.append(trade)
        
        # Clear older trades (>30s = 30000ms)
        expiry_time = (current_time * 1000) - 30000
        while self.recent_trades and self.recent_trades[0].get('timestamp', 0) < expiry_time:
            self.recent_trades.popleft()

# Global instance
_order_flow_cache = OrderFlowCache()

@dataclass
class FeatureSet:
    """Container for all computed features from a single candle update."""
    # --- Price ---
    close: float = 0.0
    prev_close: float = 0.0
    high: float = 0.0
    low: float = 0.0

    # --- EMA ---
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_trend: int = 0
    ema_cross_up: bool = False
    ema_cross_down: bool = False

    # --- RSI ---
    rsi: float = 50.0

    # --- ATR ---
    atr: float = 0.0
    atr_ma50: float = 0.0
    atr_ratio: float = 1.0

    # --- NW ---
    nw_mid: float = 0.0
    nw_upper: float = 0.0
    nw_lower: float = 0.0
    nw_long_cross: bool = False
    nw_short_cross: bool = False

    # --- Volume ---
    volume_ratio: float = 1.0
    volume_spike: bool = False

    # --- BB ---
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_width_pct: float = 0.0
    bb_squeeze: bool = False

    # --- ADX / Regime ---
    adx: float = 0.0
    regime: str = "RANGING"

    # --- VWAP ---
    vwap: float = 0.0

    # --- CVD ---
    cvd_raw: float = 0.0
    cvd_slope: float = 0.0
    cvd_divergence: int = 0

    # --- Order Book (S12) ---
    ob_imbalance: float = 0.5
    ob_bid_depth: float = 0.0
    ob_ask_depth: float = 0.0

    # --- Trade Aggression (S13) ---
    trade_aggression_ratio: float = 0.5

    # --- Liquidity Wall (S14) ---
    liquidity_wall_bid: float = 0.0
    liquidity_wall_ask: float = 0.0

    # --- Liquidity Sweep (S15) ---
    liquidity_sweep_bull: bool = False
    liquidity_sweep_bear: bool = False
    sweep_sl: float = 0.0
    sweep_tp: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            k: getattr(self, k)
            for k in self.__dataclass_fields__
        }

class FeatureCache:
    """Compute indicators once, read everywhere."""

    def __init__(self) -> None:
        self._last_features: FeatureSet | None = None
        logger.info("FeatureCache initialised")

    def compute(self, df: pd.DataFrame) -> FeatureSet:
        if df is None or len(df) < cfg.EMA_SLOW + 5:
            return FeatureSet()

        MAX_ROWS = 150
        if len(df) > MAX_ROWS:
            df = df.tail(MAX_ROWS).reset_index(drop=True)

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        fs = FeatureSet()
        fs.close = float(close.iloc[-1])
        fs.prev_close = float(close.iloc[-2]) if len(close) > 1 else fs.close
        fs.high = float(high.iloc[-1])
        fs.low = float(low.iloc[-1])

        # EMA
        ema_f = ta.ema(close, length=cfg.EMA_FAST)
        ema_s = ta.ema(close, length=cfg.EMA_SLOW)
        fs.ema_fast = float(ema_f.iloc[-1]) if ema_f is not None else fs.close
        fs.ema_slow = float(ema_s.iloc[-1]) if ema_s is not None else fs.close

        if fs.ema_fast > fs.ema_slow:
            fs.ema_trend = 1
        elif fs.ema_fast < fs.ema_slow:
            fs.ema_trend = -1

        if ema_f is not None and ema_s is not None and len(ema_f) >= 2:
            f1, f0 = float(ema_f.iloc[-2]), float(ema_f.iloc[-1])
            s1, s0 = float(ema_s.iloc[-2]), float(ema_s.iloc[-1])
            fs.ema_cross_up = (f1 <= s1) and (f0 > s0)
            fs.ema_cross_down = (f1 >= s1) and (f0 < s0)

        # RSI
        rsi = ta.rsi(close, length=cfg.RSI_PERIOD)
        fs.rsi = float(rsi.iloc[-1]) if (rsi is not None and not pd.isna(rsi.iloc[-1])) else 50.0

        # ATR
        atr = ta.atr(high, low, close, length=getattr(cfg, 'SCALP_SL_ATR_PERIOD', 14))
        fs.atr = float(atr.iloc[-1]) if atr is not None and not pd.isna(atr.iloc[-1]) else 0.0

        atr_period = getattr(cfg, 'ATR_PERIOD', 14)
        atr_full = ta.atr(high, low, close, length=atr_period)
        if atr_full is not None and len(atr_full) >= 50:
            atr_ma50_series = ta.sma(atr_full, length=50)
            fs.atr_ma50 = float(atr_ma50_series.iloc[-1]) if atr_ma50_series is not None and not pd.isna(atr_ma50_series.iloc[-1]) else fs.atr
        else:
            fs.atr_ma50 = fs.atr

        fs.atr_ratio = fs.atr / fs.atr_ma50 if fs.atr_ma50 > 0 else 1.0

        # NW
        try:
            from strategy import ScalpStrategy
            atr_14 = ta.atr(high, low, close, length=14)
            curr_atr_val = float(atr_14.iloc[-1]) if atr_14 is not None and not pd.isna(atr_14.iloc[-1]) else 100.0
            dynamic_mult = max(0.5, min(2.5, curr_atr_val / 200.0))
            effective_mult = getattr(cfg, 'NW_MULT', 1.0) * dynamic_mult

            nw_mid, nw_upper, nw_lower = ScalpStrategy.nadaraya_watson_envelope(
                close.values, getattr(cfg, 'NW_BANDWIDTH', 8), effective_mult, getattr(cfg, 'NW_LOOKBACK', 150)
            )
            fs.nw_mid = float(nw_mid[-1]) if not np.isnan(nw_mid[-1]) else fs.close
            fs.nw_upper = float(nw_upper[-1]) if not np.isnan(nw_upper[-1]) else fs.close * 1.01
            fs.nw_lower = float(nw_lower[-1]) if not np.isnan(nw_lower[-1]) else fs.close * 0.99

            fs.nw_long_cross = (fs.prev_close > fs.nw_lower) and (fs.close <= fs.nw_lower)
            fs.nw_short_cross = (fs.prev_close < fs.nw_upper) and (fs.close >= fs.nw_upper)
        except Exception:
            pass

        # Volume
        vol_sma_period = getattr(cfg, 'VOL_SMA_PERIOD', 20)
        if len(volume) >= vol_sma_period + 1:
            vol_sma = volume.rolling(vol_sma_period).mean()
            avg_vol = float(vol_sma.iloc[-1])
            if avg_vol > 0:
                fs.volume_ratio = round(float(volume.iloc[-1]) / avg_vol, 2)
                fs.volume_spike = fs.volume_ratio >= getattr(cfg, 'VOL_SPIKE_MULT', 2.0)

        # BB
        bb_period = getattr(cfg, 'BB_PERIOD', 20)
        bb_std = getattr(cfg, 'BB_STD', 2.0)
        bb = ta.bbands(close, length=bb_period, std=bb_std)
        if bb is not None and not bb.empty:
            bbu = f"BBU_{bb_period}_{bb_std}"
            bbl = f"BBL_{bb_period}_{bb_std}"
            bbm = f"BBM_{bb_period}_{bb_std}"
            if bbu in bb.columns:
                fs.bb_upper = float(bb[bbu].iloc[-1])
                fs.bb_lower = float(bb[bbl].iloc[-1])
                mid = float(bb[bbm].iloc[-1])
                if mid > 0:
                    fs.bb_width_pct = round((fs.bb_upper - fs.bb_lower) / mid, 4)
                    fs.bb_squeeze = fs.bb_width_pct < getattr(cfg, 'BB_SQUEEZE_THRESHOLD', 0.015)

        # ADX
        adx_period = getattr(cfg, 'ADX_PERIOD', 14)
        adx_df = ta.adx(high, low, close, length=adx_period)
        if adx_df is not None and not adx_df.empty:
            col = f"ADX_{adx_period}"
            if col in adx_df.columns:
                val = float(adx_df[col].iloc[-1])
                if not pd.isna(val):
                    fs.adx = round(val, 1)
                    if val >= getattr(cfg, 'ADX_STRONG_TREND', 35):
                        fs.regime = "VOLATILE"
                    elif val >= getattr(cfg, 'ADX_TREND_THRESHOLD', 25):
                        # Naive trend dir using EMA
                        fs.regime = "TRENDING_UP" if fs.ema_fast > fs.ema_slow else "TRENDING_DOWN"
                    else:
                        fs.regime = "RANGING"

        # VWAP
        try:
            typical = (high + low + close) / 3
            vwap_series = (typical * volume).cumsum() / volume.cumsum()
            fs.vwap = float(vwap_series.iloc[-1])
        except Exception:
            fs.vwap = fs.close

        # CVD
        try:
            safe_range = (high - low).replace(0, np.nan).fillna(1e-10)
            buy_fraction = ((close - low) / safe_range).clip(0, 1)
            delta = volume * (2 * buy_fraction - 1)
            cvd = delta.cumsum()
            fs.cvd_raw = float(cvd.iloc[-1])
            lookback = getattr(cfg, 'CVD_LOOKBACK', 14)
            if len(cvd) >= lookback:
                w = cvd.iloc[-lookback:].values
                x = np.arange(lookback, dtype=float)
                slp = np.sum((x - x.mean()) * (w - w.mean())) / (np.sum((x - x.mean())**2) + 1e-10)
                avg_v = float(volume.iloc[-lookback:].mean())
                if avg_v > 0:
                    fs.cvd_slope = float(np.clip(slp / avg_v, -1.0, 1.0))
        except:
            pass

        # Order Book Imbalance smoothed over 10 snapshots (S12)
        try:
            bid_vol_total = 0.0
            ask_vol_total = 0.0
            # Load latest from MarketState if we need to update cache
            from market_state import MarketState
            ob = getattr(MarketState, 'order_book', {})
            if ob:
                _order_flow_cache.add_snapshot(ob)
            
            if _order_flow_cache.book_snapshots:
                for snap in _order_flow_cache.book_snapshots:
                    bids = snap.get("bids", [])
                    asks = snap.get("asks", [])
                    for lvl in bids[:10]:
                        qty = float(lvl[1]) if isinstance(lvl, (list, tuple)) else float(lvl.get("qty", 0))
                        bid_vol_total += qty
                    for lvl in asks[:10]:
                        qty = float(lvl[1]) if isinstance(lvl, (list, tuple)) else float(lvl.get("qty", 0))
                        ask_vol_total += qty
                
                fs.ob_bid_depth = bid_vol_total
                fs.ob_ask_depth = ask_vol_total
                total = bid_vol_total + ask_vol_total
                if total > 0:
                    fs.ob_imbalance = bid_vol_total / total
        except Exception as ob_err:
            logger.debug(f"OB Imbalance calc error: {ob_err}")

        # Trade Aggression (S13)
        try:
            buy_vol = 0.0
            sell_vol = 0.0
            for t in _order_flow_cache.recent_trades:
                q = float(t.get('qty', t.get('q', 0)))
                # binance: is_buyer_maker=True means trade was a limit buy matching market sell => seller was aggressive.
                if t.get('is_buyer_maker', t.get('m', False)):
                    sell_vol += q
                else:
                    buy_vol += q
            total_vol = buy_vol + sell_vol
            if total_vol > 0:
                fs.trade_aggression_ratio = buy_vol / total_vol
        except Exception as trd_err:
            logger.debug(f"Trade Aggression calc error: {trd_err}")

        # Liquidity Sweep (S15)
        try:
            support = float(df['low'].tail(20).min())
            resistance = float(df['high'].tail(20).max())
            avg_vol20 = float(df['volume'].tail(20).mean())
            curr_vol = float(volume.iloc[-1])
            vol_ratio = (curr_vol / avg_vol20) if avg_vol20 > 0 else 0
            
            # bull sweep check: wick low, close > support
            # fake sweep guard: check wick vs atr
            lower_wick = min(fs.close, float(df['open'].iloc[-1])) - fs.low
            upper_wick = fs.high - max(fs.close, float(df['open'].iloc[-1]))

            if fs.atr > 0:
                 # Bull
                 if fs.low < support and fs.close > support and vol_ratio > 1.5 and fs.ob_imbalance > 0.55 and fs.regime != "TRENDING_DOWN":
                     if lower_wick > fs.atr * 0.1:
                         fs.liquidity_sweep_bull = True
                         fs.sweep_sl = fs.low - (fs.atr * 0.5)
                         fs.sweep_tp = fs.vwap
                 
                 # Bear
                 if fs.high > resistance and fs.close < resistance and vol_ratio > 1.5 and fs.ob_imbalance < 0.45 and fs.regime != "TRENDING_UP":
                     if upper_wick > fs.atr * 0.1:
                         fs.liquidity_sweep_bear = True
                         fs.sweep_sl = fs.high + (fs.atr * 0.5)
                         fs.sweep_tp = fs.vwap
        except Exception as swp_err:
            logger.debug(f"Sweep calc error: {swp_err}")

        self._last_features = fs
        return fs

    @property
    def last(self) -> FeatureSet | None:
        return self._last_features

def get_order_flow_cache() -> OrderFlowCache:
    return _order_flow_cache
