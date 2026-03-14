"""Alpha-Scalp Bot – Alpha Engine Module.

Multi-signal voting system that generates independent BUY/SELL/HOLD votes
for each signal source. Each vote includes direction, strength (0-1), and
a human-readable reason.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import httpx
from loguru import logger

if TYPE_CHECKING:
    from feature_cache import FeatureSet

@dataclass
class Vote:
    direction: str
    strength: float
    reason: str
    
    def __post_init__(self):
        if self.direction not in ("BUY", "SELL", "HOLD"):
            raise ValueError(f"Invalid direction: {self.direction}")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"Strength must be 0-1: {self.strength}")
    
    def to_score(self) -> int:
        return {"BUY": 1, "SELL": -1, "HOLD": 0}[self.direction]

@dataclass
class AlphaVotes:
    ema_cross: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No EMA cross"))
    rsi_zone: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "RSI neutral"))
    macd_cross: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No MACD cross"))
    bb_bounce: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No BB bounce"))
    bb_squeeze: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No BB squeeze"))
    vwap_cross: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No VWAP cross"))
    obv_trend: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "OBV neutral"))
    volume_spike: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No volume spike"))
    swing_bias: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "Swing neutral"))
    funding_bias: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "Funding neutral"))
    mtf_bias: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "MTF neutral"))
    
    # Extended fields
    nw_signal: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "NW neutral"))
    adx_filter: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "ADX neutral"))
    ob_imbalance: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "OB neutral"))
    trade_aggression: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "TA neutral"))
    liquidity_wall: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "Wall neutral"))
    liquidity_sweep: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "Sweep neutral"))
    
    def as_dict(self) -> dict[str, int]:
        return {
            "ema_cross": self.ema_cross.to_score(),
            "rsi_zone": self.rsi_zone.to_score(),
            "macd_cross": self.macd_cross.to_score(),
            "bb_bounce": self.bb_bounce.to_score(),
            "bb_squeeze": self.bb_squeeze.to_score(),
            "vwap_cross": self.vwap_cross.to_score(),
            "obv_trend": self.obv_trend.to_score(),
            "volume_spike": self.volume_spike.to_score(),
            "swing_bias": self.swing_bias.to_score(),
            "funding_bias": self.funding_bias.to_score(),
            "mtf_bias": self.mtf_bias.to_score(),
            "nw_signal": self.nw_signal.to_score(),
            "adx_filter": self.adx_filter.to_score(),
            "ob_imbalance": self.ob_imbalance.to_score(),
            "trade_aggression": self.trade_aggression.to_score(),
            "liquidity_wall": self.liquidity_wall.to_score(),
            "liquidity_sweep": self.liquidity_sweep.to_score(),
        }
    
    def get_all_votes(self) -> dict[str, Vote]:
        return {
            "ema_cross": self.ema_cross,
            "rsi_zone": self.rsi_zone,
            "macd_cross": self.macd_cross,
            "bb_bounce": self.bb_bounce,
            "bb_squeeze": self.bb_squeeze,
            "vwap_cross": self.vwap_cross,
            "obv_trend": self.obv_trend,
            "volume_spike": self.volume_spike,
            "swing_bias": self.swing_bias,
            "funding_bias": self.funding_bias,
            "mtf_bias": self.mtf_bias,
            "nw_signal": self.nw_signal,
            "adx_filter": self.adx_filter,
            "ob_imbalance": self.ob_imbalance,
            "trade_aggression": self.trade_aggression,
            "liquidity_wall": self.liquidity_wall,
            "liquidity_sweep": self.liquidity_sweep,
        }

class FundingRateCache:
    def __init__(self, ttl_seconds: int = 8 * 60 * 60):
        self._cache: dict[str, dict] = {}
        self._ttl = ttl_seconds
    
    def get(self, symbol: str) -> Optional[float]:
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        if time.time() - entry["timestamp"] > self._ttl:
            del self._cache[symbol]
            return None
        return entry["rate"]
    
    def set(self, symbol: str, rate: float) -> None:
        self._cache[symbol] = {"rate": rate, "timestamp": time.time()}

_funding_cache = FundingRateCache()

class AlphaEngine:
    def __init__(self):
        self.http_client: Optional[httpx.AsyncClient] = None
        self._sweep_cooldown = 0
    
    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        cached = _funding_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            binance_symbol = symbol.replace("/", "").replace("_", "")
            url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={binance_symbol}"
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
            rate = float(data.get("lastFundingRate", 0))
            _funding_cache.set(symbol, rate)
            return rate
        except Exception as e:
            return None
    
    def generate_funding_bias_vote(self, rate: Optional[float]) -> Vote:
        if rate is None:
            return Vote("HOLD", 0.0, "Funding rate unavailable")
        if rate <= -0.0005:
            return Vote("BUY", 0.3, "Negative funding, shorts crowded")
        elif rate >= 0.0005:
            return Vote("SELL", 0.3, "High funding, longs crowded")
        return Vote("HOLD", 0.0, "Neutral funding")
    
    def generate_votes(
        self,
        features: "FeatureSet",
        funding_rate: Optional[float] = None,
        swing_vote: Optional[Vote] = None,
        mtf_vote: Optional[Vote] = None,
    ) -> AlphaVotes:
        if getattr(self, "_sweep_cooldown", 0) > 0:
            self._sweep_cooldown -= 1
        votes = AlphaVotes()
        
        # Features mapping fallback mapping
        f_ema_fast = getattr(features, 'ema_fast', 0)
        f_ema_slow = getattr(features, 'ema_slow', 0)
        f_rsi = getattr(features, 'rsi', 50)
        f_price = getattr(features, 'close', 0)
        
        # EMA
        if f_ema_fast > f_ema_slow * 1.001:
            votes.ema_cross = Vote("BUY", 0.8, "EMA bullish cross")
        elif f_ema_fast < f_ema_slow * 0.999:
            votes.ema_cross = Vote("SELL", 0.8, "EMA bearish cross")
            
        # RSI
        if f_rsi < 35:
            votes.rsi_zone = Vote("BUY", 0.7, "RSI oversold")
        elif f_rsi > 65:
            votes.rsi_zone = Vote("SELL", 0.7, "RSI overbought")
            
        # NW (Example derived from features)
        if getattr(features, 'nw_long_cross', False):
            votes.nw_signal = Vote("BUY", 0.8, "NW lower cross")
        elif getattr(features, 'nw_short_cross', False):
            votes.nw_signal = Vote("SELL", 0.8, "NW upper cross")

        # Bias
        if swing_vote:
            votes.swing_bias = swing_vote
        if funding_rate is not None:
            votes.funding_bias = self.generate_funding_bias_vote(funding_rate)
        if mtf_vote:
            votes.mtf_bias = mtf_vote
            
        # S12: Order Book Imbalance
        ob = getattr(features, 'ob_imbalance', 0.5)
        if ob > 0.65:
            votes.ob_imbalance = Vote("BUY", 0.6, "Order book bid-heavy")
        elif ob < 0.35:
            votes.ob_imbalance = Vote("SELL", 0.6, "Order book ask-heavy")

        # S13: Trade Aggression
        ta_ratio = getattr(features, 'trade_aggression_ratio', 0.5)
        if ta_ratio > 0.60:
            votes.trade_aggression = Vote("BUY", 0.7, "Aggressive buyers")
        elif ta_ratio < 0.40:
            votes.trade_aggression = Vote("SELL", 0.7, "Aggressive sellers")
            
        # S14: Liquidity Wall
        # Calculate Wall from Current MarketState OrderBook directly for more precision
        try:
            from market_state import MarketState
            ob_data = getattr(MarketState, 'order_book', {})
            bids = ob_data.get("bids", [])[:20]
            asks = ob_data.get("asks", [])[:20]
            
            if bids and asks:
                bid_qtys = [float(lvl[1] if isinstance(lvl, (list, tuple)) else lvl.get("qty", 0)) for lvl in bids]
                ask_qtys = [float(lvl[1] if isinstance(lvl, (list, tuple)) else lvl.get("qty", 0)) for lvl in asks]
                
                bid_prices = [float(lvl[0] if isinstance(lvl, (list, tuple)) else lvl.get("price", 0)) for lvl in bids]
                ask_prices = [float(lvl[0] if isinstance(lvl, (list, tuple)) else lvl.get("price", 0)) for lvl in asks]
                
                bid_mean = (sum(bid_qtys) - max(bid_qtys)) / max(len(bid_qtys)-1, 1)
                ask_mean = (sum(ask_qtys) - max(ask_qtys)) / max(len(ask_qtys)-1, 1)
                
                max_bid_idx = bid_qtys.index(max(bid_qtys))
                max_ask_idx = ask_qtys.index(max(ask_qtys))
                
                bid_wall = bid_qtys[max_bid_idx] > 3 * bid_mean
                ask_wall = ask_qtys[max_ask_idx] > 3 * ask_mean
                
                # Within 0.3% of current price
                if bid_wall and (f_price - bid_prices[max_bid_idx]) / f_price <= 0.003:
                    votes.liquidity_wall = Vote("BUY", 0.5, "Bid wall support")
                elif ask_wall and (ask_prices[max_ask_idx] - f_price) / f_price <= 0.003:
                    votes.liquidity_wall = Vote("SELL", 0.5, "Ask wall resistance")
        except Exception as e:
            logger.debug(f"Liquidity wall check failed: {e}")

        # S15: Liquidity Sweep
        if getattr(features, 'liquidity_sweep_bull', False):
            if getattr(self, "_sweep_cooldown", 0) > 0:
                votes.liquidity_sweep = Vote("HOLD", 0.0, "Sweep cooldown")
            else:
                votes.liquidity_sweep = Vote("BUY", 0.85, "Bullish liquidity sweep (TP/SL override)")
                self._sweep_cooldown = 2
        elif getattr(features, 'liquidity_sweep_bear', False):
            if getattr(self, "_sweep_cooldown", 0) > 0:
                votes.liquidity_sweep = Vote("HOLD", 0.0, "Sweep cooldown")
            else:
                votes.liquidity_sweep = Vote("SELL", 0.85, "Bearish liquidity sweep (TP/SL override)")
                self._sweep_cooldown = 2
            
        return votes

    async def generate_votes_with_funding(
        self,
        features: "FeatureSet",
        symbol: str,
        swing_vote: Optional[Vote] = None,
        mtf_vote: Optional[Vote] = None,
    ) -> AlphaVotes:
        funding_rate = await self.get_funding_rate(symbol)
        return self.generate_votes(
            features=features,
            funding_rate=funding_rate,
            swing_vote=swing_vote,
            mtf_vote=mtf_vote,
        )
