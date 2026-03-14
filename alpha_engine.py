"""Alpha-Scalp Bot – Alpha Engine Module.

Multi-signal voting system that generates independent BUY/SELL/HOLD votes
for each signal source. Each vote includes direction, strength (0-1), and
a human-readable reason.

Signals:
- ema_cross: EMA fast/slow crossover
- rsi_zone: RSI oversold/overbought zones
- macd_cross: MACD line/signal crossover
- bb_bounce: Bollinger Band mean reversion
- bb_squeeze: Bollinger squeeze breakout
- vwap_cross: VWAP crossover
- obv_trend: OBV trend direction
- volume_spike: Volume spike confirmation
- swing_bias: 4h swing strategy bias
- funding_bias: Funding rate sentiment (P1-7)
- mtf_bias: 15m MTF confirmation (P1-8)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from loguru import logger

# config imported but not used in this module - kept for potential future use
# import config as cfg


# =============================================================================
# Vote Dataclass
# =============================================================================

@dataclass
class Vote:
    """Single signal vote with direction, strength, and reason.
    
    Attributes:
        direction: "BUY" | "SELL" | "HOLD"
        strength: 0.0 to 1.0 (confidence in this vote)
        reason: Human-readable explanation
    """
    direction: str
    strength: float
    reason: str
    
    def __post_init__(self):
        if self.direction not in ("BUY", "SELL", "HOLD"):
            raise ValueError(f"Invalid direction: {self.direction}")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"Strength must be 0-1: {self.strength}")
    
    def to_score(self) -> int:
        """Convert to numeric score for aggregation.
        
        Returns:
            +1 for BUY, -1 for SELL, 0 for HOLD
        """
        return {"BUY": 1, "SELL": -1, "HOLD": 0}[self.direction]


# =============================================================================
# AlphaVotes Container
# =============================================================================

@dataclass
class AlphaVotes:
    """Container for all signal votes.
    
    Each signal independently votes BUY/SELL/HOLD with strength 0-1.
    Votes are aggregated by SignalScoring with regime-specific weights.
    """
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
    
    def as_dict(self) -> dict[str, int]:
        """Convert votes to dictionary of numeric scores.
        
        Returns:
            Dict mapping signal name to score (+1/-1/0)
        """
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
        }
    
    def get_all_votes(self) -> dict[str, Vote]:
        """Get all votes as Vote objects.
        
        Returns:
            Dict mapping signal name to Vote object
        """
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
        }


# =============================================================================
# Funding Rate Cache (P1-7)
# =============================================================================

class FundingRateCache:
    """In-memory cache for funding rate data.
    
    Caches funding rate for 8 hours to avoid API spam.
    Uses Binance premiumIndex endpoint.
    """
    
    def __init__(self, ttl_seconds: int = 8 * 60 * 60):
        self._cache: dict[str, dict] = {}
        self._ttl = ttl_seconds
    
    def get(self, symbol: str) -> Optional[float]:
        """Get cached funding rate if not expired.
        
        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            
        Returns:
            Funding rate or None if expired/missing
        """
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        
        if time.time() - entry["timestamp"] > self._ttl:
            del self._cache[symbol]
            return None
        
        return entry["rate"]
    
    def set(self, symbol: str, rate: float) -> None:
        """Cache funding rate with current timestamp.
        
        Args:
            symbol: Trading pair
            rate: Funding rate value
        """
        self._cache[symbol] = {
            "rate": rate,
            "timestamp": time.time()
        }
        logger.debug("Cached funding rate for {}: {}", symbol, rate)


# Global funding rate cache instance
_funding_cache = FundingRateCache()


# =============================================================================
# Alpha Engine
# =============================================================================

class AlphaEngine:
    """Multi-signal voting engine.
    
    Generates independent votes from multiple signal sources.
    Each signal votes BUY/SELL/HOLD with strength 0-1.
    """
    
    def __init__(self):
        self.http_client: Optional[httpx.AsyncClient] = None
    
    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Fetch funding rate from Binance.
        
        Uses Binance premiumIndex endpoint:
        GET /fapi/v1/premiumIndex?symbol={SYMBOL}
        
        Args:
            symbol: Trading pair (e.g., "BTCUSDT")
            
        Returns:
            Funding rate or None on error
        """
        # Check cache first
        cached = _funding_cache.get(symbol)
        if cached is not None:
            logger.debug("Using cached funding rate for {}: {}", symbol, cached)
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
            
            logger.info("Fetched funding rate for {}: {}", symbol, rate)
            return rate
            
        except Exception as e:
            logger.warning("Failed to fetch funding rate for {}: {}", symbol, e)
            return None
    
    def generate_funding_bias_vote(self, rate: Optional[float]) -> Vote:
        """Generate funding bias vote based on funding rate.
        
        Voting logic:
        - rate < -0.0005 -> Vote("BUY", strength=0.3, "Negative funding, shorts crowded")
        - rate > 0.0005 -> Vote("SELL", strength=0.3, "High funding, longs crowded")
        - else -> Vote("HOLD", strength=0.0, "Neutral funding")
        
        Args:
            rate: Funding rate value
            
        Returns:
            Vote object with direction, strength, reason
        """
        if rate is None:
            return Vote("HOLD", 0.0, "Funding rate unavailable")
        
        if rate <= -0.0005:
            return Vote(
                "BUY",
                strength=0.3,
                reason="Negative funding, shorts crowded"
            )
        elif rate >= 0.0005:
            return Vote(
                "SELL",
                strength=0.3,
                reason="High funding, longs crowded"
            )
        else:
            return Vote("HOLD", strength=0.0, reason="Neutral funding")
    
    def generate_votes(
        self,
        features: "FeatureSet",
        funding_rate: Optional[float] = None,
        swing_vote: Optional[Vote] = None,
        mtf_vote: Optional[Vote] = None,
    ) -> AlphaVotes:
        """Generate all signal votes.
        
        Args:
            features: FeatureSet with all computed indicators
            funding_rate: Optional pre-fetched funding rate
            swing_vote: Optional vote from swing strategy (4h bias)
            mtf_vote: Optional vote from MTF strategy (15m confirmation)
            
        Returns:
            AlphaVotes container with all signal votes
        """
        votes = AlphaVotes()
        
        # ===== Core Signals =====
        
        # EMA Cross
        if hasattr(features, 'ema_fast') and hasattr(features, 'ema_slow'):
            if features.ema_fast > features.ema_slow * 1.001:
                votes.ema_cross = Vote("BUY", 0.8, "EMA bullish cross")
            elif features.ema_fast < features.ema_slow * 0.999:
                votes.ema_cross = Vote("SELL", 0.8, "EMA bearish cross")
        
        # RSI Zone
        if hasattr(features, 'rsi'):
            if features.rsi < 35:
                votes.rsi_zone = Vote("BUY", 0.7, "RSI oversold")
            elif features.rsi > 65:
                votes.rsi_zone = Vote("SELL", 0.7, "RSI overbought")
        
        # MACD Cross
        if hasattr(features, 'macd') and hasattr(features, 'macd_signal'):
            if features.macd > features.macd_signal * 1.01:
                votes.macd_cross = Vote("BUY", 0.6, "MACD bullish")
            elif features.macd < features.macd_signal * 0.99:
                votes.macd_cross = Vote("SELL", 0.6, "MACD bearish")
        
        # BB Bounce
        if hasattr(features, 'bb_lower') and hasattr(features, 'bb_upper'):
            price = getattr(features, 'close', 0)
            if price < features.bb_lower:
                votes.bb_bounce = Vote("BUY", 0.6, "Price below BB lower")
            elif price > features.bb_upper:
                votes.bb_bounce = Vote("SELL", 0.6, "Price above BB upper")
        
        # BB Squeeze
        if hasattr(features, 'bb_squeeze') and features.bb_squeeze:
            votes.bb_squeeze = Vote("BUY", 0.5, "BB squeeze detected")
        
        # VWAP Cross
        if hasattr(features, 'vwap'):
            price = getattr(features, 'close', 0)
            if price > features.vwap * 1.001:
                votes.vwap_cross = Vote("BUY", 0.5, "Price above VWAP")
            elif price < features.vwap * 0.999:
                votes.vwap_cross = Vote("SELL", 0.5, "Price below VWAP")
        
        # OBV Trend
        if hasattr(features, 'obv_slope'):
            if features.obv_slope > 0:
                votes.obv_trend = Vote("BUY", 0.5, "OBV uptrend")
            elif features.obv_slope < 0:
                votes.obv_trend = Vote("SELL", 0.5, "OBV downtrend")
        
        # Volume Spike
        if hasattr(features, 'volume_ratio'):
            if features.volume_ratio > 2.0:
                votes.volume_spike = Vote("BUY", 0.4, "Volume spike confirmation")
        
        # ===== Bias Signals =====
        
        # Swing Bias (4h)
        if swing_vote is not None:
            votes.swing_bias = swing_vote
        
        # Funding Bias (P1-7)
        if funding_rate is not None:
            votes.funding_bias = self.generate_funding_bias_vote(funding_rate)
        
        # MTF Bias (15m, P1-8)
        if mtf_vote is not None:
            votes.mtf_bias = mtf_vote
        
        return votes
    
    async def generate_votes_with_funding(
        self,
        features: "FeatureSet",
        symbol: str,
        swing_vote: Optional[Vote] = None,
        mtf_vote: Optional[Vote] = None,
    ) -> AlphaVotes:
        """Generate votes with automatic funding rate fetch.
        
        Convenience method that fetches funding rate and generates all votes.
        
        Args:
            features: FeatureSet with indicators
            symbol: Trading pair
            swing_vote: Optional swing strategy vote
            mtf_vote: Optional MTF vote
            
        Returns:
            AlphaVotes with all signals including funding
        """
        funding_rate = await self.get_funding_rate(symbol)
        return self.generate_votes(
            features=features,
            funding_rate=funding_rate,
            swing_vote=swing_vote,
            mtf_vote=mtf_vote,
        )
