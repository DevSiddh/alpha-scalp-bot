"""Alpha-Scalp Bot – Alpha Engine Module.

Multi-signal voting system that generates independent BUY/SELL/HOLD votes
for each signal source. Each vote includes direction, strength (0-1), and
a human-readable reason.

FIXED (2026-03-14):
- Wired all 17 signals: MACD, BB bounce/squeeze, VWAP cross, OBV trend,
  volume spike, adx_filter were previously uncomputed (always HOLD).
- Added per-vote debug logging before returning VoteSet.
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
    ema_cross:        Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No EMA cross"))
    rsi_zone:         Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "RSI neutral"))
    macd_cross:       Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No MACD cross"))
    bb_bounce:        Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No BB bounce"))
    bb_squeeze:       Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No BB squeeze"))
    vwap_cross:       Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No VWAP cross"))
    obv_trend:        Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "OBV neutral"))
    volume_spike:     Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "No volume spike"))
    swing_bias:       Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "Swing neutral"))
    funding_bias:     Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "Funding neutral"))
    mtf_bias:         Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "MTF neutral"))
    nw_signal:        Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "NW neutral"))
    adx_filter:       Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "ADX neutral"))
    ob_imbalance:     Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "OB neutral"))
    trade_aggression: Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "TA neutral"))
    liquidity_wall:   Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "Wall neutral"))
    liquidity_sweep:  Vote = field(default_factory=lambda: Vote("HOLD", 0.0, "Sweep neutral"))

    def as_dict(self) -> dict[str, int]:
        return {
            "ema_cross":        self.ema_cross.to_score(),
            "rsi_zone":         self.rsi_zone.to_score(),
            "macd_cross":       self.macd_cross.to_score(),
            "bb_bounce":        self.bb_bounce.to_score(),
            "bb_squeeze":       self.bb_squeeze.to_score(),
            "vwap_cross":       self.vwap_cross.to_score(),
            "obv_trend":        self.obv_trend.to_score(),
            "volume_spike":     self.volume_spike.to_score(),
            "swing_bias":       self.swing_bias.to_score(),
            "funding_bias":     self.funding_bias.to_score(),
            "mtf_bias":         self.mtf_bias.to_score(),
            "nw_signal":        self.nw_signal.to_score(),
            "adx_filter":       self.adx_filter.to_score(),
            "ob_imbalance":     self.ob_imbalance.to_score(),
            "trade_aggression": self.trade_aggression.to_score(),
            "liquidity_wall":   self.liquidity_wall.to_score(),
            "liquidity_sweep":  self.liquidity_sweep.to_score(),
        }

    def get_all_votes(self) -> dict[str, Vote]:
        return {
            "ema_cross":        self.ema_cross,
            "rsi_zone":         self.rsi_zone,
            "macd_cross":       self.macd_cross,
            "bb_bounce":        self.bb_bounce,
            "bb_squeeze":       self.bb_squeeze,
            "vwap_cross":       self.vwap_cross,
            "obv_trend":        self.obv_trend,
            "volume_spike":     self.volume_spike,
            "swing_bias":       self.swing_bias,
            "funding_bias":     self.funding_bias,
            "mtf_bias":         self.mtf_bias,
            "nw_signal":        self.nw_signal,
            "adx_filter":       self.adx_filter,
            "ob_imbalance":     self.ob_imbalance,
            "trade_aggression": self.trade_aggression,
            "liquidity_wall":   self.liquidity_wall,
            "liquidity_sweep":  self.liquidity_sweep,
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
        self._sweep_cooldown: int = 0
        # OBV state — track last close + cumulative OBV across calls
        self._obv_prev: float = 0.0
        self._obv_direction: int = 0  # +1 rising, -1 falling, 0 flat

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
        except Exception:
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
        debug: bool = False,
    ) -> AlphaVotes:
        """Compute all 17 signal votes from a FeatureSet.

        Every signal that was previously left as default HOLD is now computed.
        When debug=True, every vote direction+strength is logged.
        """
        if getattr(self, "_sweep_cooldown", 0) > 0:
            self._sweep_cooldown -= 1

        votes = AlphaVotes()

        # ── convenience aliases ────────────────────────────────────────────
        f_ema_fast = getattr(features, "ema_fast", 0.0)
        f_ema_slow = getattr(features, "ema_slow", 0.0)
        f_rsi      = getattr(features, "rsi", 50.0)
        f_close    = getattr(features, "close", 0.0)
        f_prev     = getattr(features, "prev_close", f_close)
        f_vwap     = getattr(features, "vwap", 0.0)
        f_atr      = getattr(features, "atr", 0.0)
        f_adx      = getattr(features, "adx", 0.0)
        f_regime   = getattr(features, "regime", "RANGING")
        f_bb_upper = getattr(features, "bb_upper", 0.0)
        f_bb_lower = getattr(features, "bb_lower", 0.0)
        f_vol_ratio = getattr(features, "volume_ratio", 1.0)
        f_vol_spike = getattr(features, "volume_spike", False)
        f_bb_squeeze = getattr(features, "bb_squeeze", False)
        f_cvd_slope  = getattr(features, "cvd_slope", 0.0)

        # ── S1: EMA cross ──────────────────────────────────────────────────
        if f_ema_fast > f_ema_slow * 1.001:
            votes.ema_cross = Vote("BUY", 0.8, "EMA bullish")
        elif f_ema_fast < f_ema_slow * 0.999:
            votes.ema_cross = Vote("SELL", 0.8, "EMA bearish")

        # ── S2: RSI zone ───────────────────────────────────────────────────
        if f_rsi < 35:
            votes.rsi_zone = Vote("BUY", 0.7, f"RSI oversold ({f_rsi:.1f})")
        elif f_rsi > 65:
            votes.rsi_zone = Vote("SELL", 0.7, f"RSI overbought ({f_rsi:.1f})")

        # ── S3: MACD cross (derived from EMA9/21 delta momentum) ───────────
        # Proxy: fast EMA accelerating vs slow — sign change of (fast-slow)
        # Uses ema_cross_up / ema_cross_down flags computed in feature_cache
        ema_cross_up   = getattr(features, "ema_cross_up", False)
        ema_cross_down = getattr(features, "ema_cross_down", False)
        if ema_cross_up:
            votes.macd_cross = Vote("BUY", 0.75, "EMA/MACD bullish cross")
        elif ema_cross_down:
            votes.macd_cross = Vote("SELL", 0.75, "EMA/MACD bearish cross")
        else:
            # Momentum direction: MACD-line proxy = fast - slow
            macd_val = f_ema_fast - f_ema_slow
            if f_ema_slow > 0:
                macd_pct = macd_val / f_ema_slow
                if macd_pct > 0.002:
                    votes.macd_cross = Vote("BUY", 0.5, f"MACD above zero ({macd_pct:.4f})")
                elif macd_pct < -0.002:
                    votes.macd_cross = Vote("SELL", 0.5, f"MACD below zero ({macd_pct:.4f})")

        # ── S4: BB bounce ──────────────────────────────────────────────────
        if f_bb_lower > 0 and f_bb_upper > 0:
            bb_range = f_bb_upper - f_bb_lower
            if bb_range > 0:
                bb_pos = (f_close - f_bb_lower) / bb_range  # 0=lower, 1=upper
                if bb_pos < 0.15:  # close near lower band
                    votes.bb_bounce = Vote("BUY", 0.65, f"BB lower bounce (pos={bb_pos:.2f})")
                elif bb_pos > 0.85:  # close near upper band
                    votes.bb_bounce = Vote("SELL", 0.65, f"BB upper bounce (pos={bb_pos:.2f})")

        # ── S5: BB squeeze breakout ────────────────────────────────────────
        if f_bb_squeeze:
            # Squeeze active — directional bias from EMA trend
            if f_ema_fast > f_ema_slow:
                votes.bb_squeeze = Vote("BUY", 0.6, "BB squeeze, EMA bullish")
            else:
                votes.bb_squeeze = Vote("SELL", 0.6, "BB squeeze, EMA bearish")

        # ── S6: VWAP cross ─────────────────────────────────────────────────
        if f_vwap > 0 and f_close > 0:
            vwap_pct = (f_close - f_vwap) / f_vwap
            if f_prev < f_vwap <= f_close:  # just crossed above
                votes.vwap_cross = Vote("BUY", 0.7, "Price crossed above VWAP")
            elif f_prev > f_vwap >= f_close:  # just crossed below
                votes.vwap_cross = Vote("SELL", 0.7, "Price crossed below VWAP")
            elif vwap_pct > 0.002:  # above VWAP — mild bullish
                votes.vwap_cross = Vote("BUY", 0.4, f"Price above VWAP ({vwap_pct:.4f})")
            elif vwap_pct < -0.002:  # below VWAP — mild bearish
                votes.vwap_cross = Vote("SELL", 0.4, f"Price below VWAP ({vwap_pct:.4f})")

        # ── S7: OBV trend (via CVD slope as proxy) ─────────────────────────
        # CVD slope is normalised to [-1, 1] in feature_cache
        if f_cvd_slope > 0.25:
            votes.obv_trend = Vote("BUY", min(f_cvd_slope, 0.8), f"CVD/OBV rising ({f_cvd_slope:.2f})")
        elif f_cvd_slope < -0.25:
            votes.obv_trend = Vote("SELL", min(abs(f_cvd_slope), 0.8), f"CVD/OBV falling ({f_cvd_slope:.2f})")

        # ── S8: Volume spike ───────────────────────────────────────────────
        if f_vol_spike:
            # Spike alone is directional only with price movement
            if f_close > f_prev:
                votes.volume_spike = Vote("BUY", 0.6, f"Volume spike + up candle ({f_vol_ratio:.1f}x)")
            else:
                votes.volume_spike = Vote("SELL", 0.6, f"Volume spike + down candle ({f_vol_ratio:.1f}x)")
        elif f_vol_ratio >= 1.5:
            # Elevated volume — weaker signal
            if f_close > f_prev:
                votes.volume_spike = Vote("BUY", 0.35, f"Elevated volume + up ({f_vol_ratio:.1f}x)")
            elif f_close < f_prev:
                votes.volume_spike = Vote("SELL", 0.35, f"Elevated volume + down ({f_vol_ratio:.1f}x)")

        # ── S9: Swing bias (injected externally) ───────────────────────────
        if swing_vote:
            votes.swing_bias = swing_vote

        # ── S10: Funding bias ──────────────────────────────────────────────
        if funding_rate is not None:
            votes.funding_bias = self.generate_funding_bias_vote(funding_rate)

        # ── S11: MTF bias (injected externally) ────────────────────────────
        if mtf_vote:
            votes.mtf_bias = mtf_vote

        # ── S12: NW envelope cross ─────────────────────────────────────────
        if getattr(features, "nw_long_cross", False):
            votes.nw_signal = Vote("BUY", 0.8, "NW lower band cross")
        elif getattr(features, "nw_short_cross", False):
            votes.nw_signal = Vote("SELL", 0.8, "NW upper band cross")
        else:
            # Positional bias: close vs NW mid
            nw_mid = getattr(features, "nw_mid", 0.0)
            if nw_mid > 0 and f_close > 0:
                nw_pct = (f_close - nw_mid) / nw_mid
                if nw_pct > 0.003:
                    votes.nw_signal = Vote("BUY", 0.4, f"Close above NW mid ({nw_pct:.4f})")
                elif nw_pct < -0.003:
                    votes.nw_signal = Vote("SELL", 0.4, f"Close below NW mid ({nw_pct:.4f})")

        # ── S13: ADX filter ────────────────────────────────────────────────
        # ADX confirms trend strength — agree with EMA direction when trending
        import config as cfg
        adx_thresh = getattr(cfg, "ADX_TREND_THRESHOLD", 25.0)
        if f_adx >= adx_thresh:
            if f_ema_fast > f_ema_slow:
                votes.adx_filter = Vote("BUY", min(f_adx / 100.0, 0.8), f"ADX trend BUY ({f_adx:.1f})")
            else:
                votes.adx_filter = Vote("SELL", min(f_adx / 100.0, 0.8), f"ADX trend SELL ({f_adx:.1f})")
        else:
            # Low ADX = ranging — mild counter-signal (mean reversion)
            if f_adx < 15 and f_bb_lower > 0:
                bb_range = f_bb_upper - f_bb_lower
                if bb_range > 0:
                    bb_pos = (f_close - f_bb_lower) / bb_range
                    if bb_pos < 0.3:
                        votes.adx_filter = Vote("BUY", 0.3, f"ADX low+near BB low ({f_adx:.1f})")
                    elif bb_pos > 0.7:
                        votes.adx_filter = Vote("SELL", 0.3, f"ADX low+near BB high ({f_adx:.1f})")

        # ── S14: Order Book Imbalance ──────────────────────────────────────
        ob = getattr(features, "ob_imbalance", 0.5)
        if ob > 0.65:
            votes.ob_imbalance = Vote("BUY", 0.6, f"OB bid-heavy ({ob:.2f})")
        elif ob < 0.35:
            votes.ob_imbalance = Vote("SELL", 0.6, f"OB ask-heavy ({ob:.2f})")

        # ── S15: Trade Aggression ──────────────────────────────────────────
        ta_ratio = getattr(features, "trade_aggression_ratio", 0.5)
        if ta_ratio > 0.60:
            votes.trade_aggression = Vote("BUY", 0.7, f"Aggressive buyers ({ta_ratio:.2f})")
        elif ta_ratio < 0.40:
            votes.trade_aggression = Vote("SELL", 0.7, f"Aggressive sellers ({ta_ratio:.2f})")

        # ── S16: Liquidity Wall (live order book — skipped in backtest) ────
        try:
            from market_state import MarketState
            ob_data = getattr(MarketState, "order_book", {})
            bids = ob_data.get("bids", [])[:20]
            asks = ob_data.get("asks", [])[:20]

            if bids and asks:
                bid_qtys = [float(lvl[1] if isinstance(lvl, (list, tuple)) else lvl.get("qty", 0)) for lvl in bids]
                ask_qtys = [float(lvl[1] if isinstance(lvl, (list, tuple)) else lvl.get("qty", 0)) for lvl in asks]
                bid_prices = [float(lvl[0] if isinstance(lvl, (list, tuple)) else lvl.get("price", 0)) for lvl in bids]
                ask_prices = [float(lvl[0] if isinstance(lvl, (list, tuple)) else lvl.get("price", 0)) for lvl in asks]

                bid_mean = (sum(bid_qtys) - max(bid_qtys)) / max(len(bid_qtys) - 1, 1)
                ask_mean = (sum(ask_qtys) - max(ask_qtys)) / max(len(ask_qtys) - 1, 1)

                max_bid_idx = bid_qtys.index(max(bid_qtys))
                max_ask_idx = ask_qtys.index(max(ask_qtys))

                bid_wall = bid_qtys[max_bid_idx] > 3 * bid_mean
                ask_wall = ask_qtys[max_ask_idx] > 3 * ask_mean

                if bid_wall and f_close > 0 and (f_close - bid_prices[max_bid_idx]) / f_close <= 0.003:
                    votes.liquidity_wall = Vote("BUY", 0.5, "Bid wall support")
                elif ask_wall and f_close > 0 and (ask_prices[max_ask_idx] - f_close) / f_close <= 0.003:
                    votes.liquidity_wall = Vote("SELL", 0.5, "Ask wall resistance")
        except Exception as e:
            logger.debug(f"Liquidity wall check failed: {e}")

        # ── S17: Liquidity Sweep ───────────────────────────────────────────
        if getattr(features, "liquidity_sweep_bull", False):
            if self._sweep_cooldown > 0:
                votes.liquidity_sweep = Vote("HOLD", 0.0, "Sweep cooldown")
            else:
                votes.liquidity_sweep = Vote("BUY", 0.85, "Bullish liquidity sweep")
                self._sweep_cooldown = 2
        elif getattr(features, "liquidity_sweep_bear", False):
            if self._sweep_cooldown > 0:
                votes.liquidity_sweep = Vote("HOLD", 0.0, "Sweep cooldown")
            else:
                votes.liquidity_sweep = Vote("SELL", 0.85, "Bearish liquidity sweep")
                self._sweep_cooldown = 2

        # ── Debug logging: every vote direction + strength ─────────────────
        if debug:
            all_votes = votes.get_all_votes()
            active = [(n, v.direction, v.strength, v.reason) for n, v in all_votes.items() if v.direction != "HOLD"]
            inactive = [n for n, v in all_votes.items() if v.direction == "HOLD"]
            logger.info("[VOTES] Active ({}/{}):", len(active), len(all_votes))
            for name, direction, strength, reason in active:
                logger.info("  {:20s} {:4s}  str={:.2f}  reason={}", name, direction, strength, reason)
            logger.debug("[VOTES] HOLD signals: {}", ", ".join(inactive))
        else:
            # Always debug-log so --debug flag in backtest can surface votes
            all_votes = votes.get_all_votes()
            active = [(n, v.direction, v.strength) for n, v in all_votes.items() if v.direction != "HOLD"]
            logger.debug(
                "[VOTES] {}/{} active: {}",
                len(active),
                len(all_votes),
                ", ".join(f"{n}={d}({s:.2f})" for n, d, s in active) or "none",
            )

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
