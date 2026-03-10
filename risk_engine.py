"""Alpha-Scalp Bot – Risk Management Engine.

Handles:
- 3 % daily drawdown kill-switch
- Position sizing (1 % equity risk per trade)
- Stop-loss / take-profit calculation
- Max open-position enforcement
- Per-loop balance caching to reduce API calls
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger

import config as cfg

if TYPE_CHECKING:
    import ccxt


class RiskEngine:
    """Centralised risk-management layer for the scalping bot."""

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    def __init__(self, exchange: ccxt.Exchange) -> None:
        self.exchange = exchange

        # Config shortcuts
        self.risk_per_trade: float = cfg.RISK_PER_TRADE
        self.daily_dd_limit: float = cfg.DAILY_DRAWDOWN_LIMIT
        self.stop_loss_pct: float = cfg.STOP_LOSS_PCT
        self.take_profit_pct: float = cfg.TAKE_PROFIT_PCT
        self.max_positions: int = cfg.MAX_OPEN_POSITIONS
        self.leverage: int = cfg.LEVERAGE
        self.symbol: str = cfg.SYMBOL

        # Daily tracking
        self.daily_start_balance: float = 0.0
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.daily_wins: int = 0
        self.kill_switch_active: bool = False

        # Trade tracker reference (set via set_trade_tracker)
        self.trade_tracker: Any | None = None

        # Balance cache (Fix 4) – avoids duplicate fetch_balance calls per loop
        self._cached_balance: float = 0.0
        self._cache_timestamp: float = 0.0
        self._cache_ttl: float = 3.0  # seconds – valid for one loop iteration

        # Initialise on first run
        self._sync_daily_balance()
        logger.info(
            "RiskEngine initialised | start_balance={:.2f} USDT | "
            "dd_limit={:.1%} | risk/trade={:.1%} | leverage={}x",
            self.daily_start_balance,
            self.daily_dd_limit,
            self.risk_per_trade,
            self.leverage,
        )

    # -----------------------------------------------------------------
    # Trade Tracker integration
    # -----------------------------------------------------------------
    def set_trade_tracker(self, tracker: Any) -> None:
        """Attach a TradeTracker instance for persistent trade logging."""
        self.trade_tracker = tracker
        logger.info("TradeTracker attached to RiskEngine")

    # -----------------------------------------------------------------
    # Balance helpers (with cache – Fix 4)
    # -----------------------------------------------------------------
    def _fetch_usdt_balance(self) -> float:
        """Return total USDT equity from Binance Futures (always fresh)."""
        try:
            bal = self.exchange.fetch_balance({"type": "future"})
            total = float(bal.get("total", {}).get("USDT", 0.0))
            # Update cache on every fresh fetch
            self._cached_balance = total
            self._cache_timestamp = time.monotonic()
            logger.debug("Fetched USDT balance: {:.4f} (cache refreshed)", total)
            return total
        except Exception as exc:
            logger.error("Failed to fetch balance: {}", exc)
            raise

    def get_cached_balance(self) -> float:
        """Return USDT balance from cache if fresh, otherwise fetch.

        Cache TTL is ~3 seconds, meaning within a single loop iteration
        (5s interval), at most ONE API call is made for balance.
        """
        now = time.monotonic()
        if (now - self._cache_timestamp) < self._cache_ttl and self._cached_balance > 0:
            logger.debug(
                "Using cached balance: {:.4f} (age={:.1f}s)",
                self._cached_balance,
                now - self._cache_timestamp,
            )
            return self._cached_balance
        return self._fetch_usdt_balance()

    def invalidate_balance_cache(self) -> None:
        """Force next balance read to hit the API."""
        self._cache_timestamp = 0.0
        logger.debug("Balance cache invalidated")

    def _sync_daily_balance(self) -> None:
        """Snapshot balance for daily drawdown tracking."""
        self.daily_start_balance = self._fetch_usdt_balance()
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_wins = 0
        self.kill_switch_active = False
        logger.info(
            "Daily balance synced: {:.2f} USDT", self.daily_start_balance
        )

    # -----------------------------------------------------------------
    # Kill Switch  (3 % daily drawdown)
    # -----------------------------------------------------------------
    def check_kill_switch(self) -> bool:
        """Return *True* if trading must stop (daily drawdown >= limit).

        Uses cached balance to avoid extra API call.
        """
        if self.kill_switch_active:
            return True

        try:
            current_balance = self.get_cached_balance()
        except Exception:
            logger.warning("Balance read failed – activating kill switch")
            self.kill_switch_active = True
            return True

        if self.daily_start_balance <= 0:
            logger.warning("Daily start balance is zero – activating kill switch")
            self.kill_switch_active = True
            return True

        drawdown = (self.daily_start_balance - current_balance) / self.daily_start_balance
        self.daily_pnl = current_balance - self.daily_start_balance

        logger.debug(
            "Kill-switch check | start={:.2f} | now={:.2f} | dd={:.2%}",
            self.daily_start_balance,
            current_balance,
            drawdown,
        )

        if drawdown >= self.daily_dd_limit:
            self.kill_switch_active = True
            logger.critical(
                "KILL SWITCH ACTIVATED | drawdown {:.2%} >= limit {:.2%}",
                drawdown,
                self.daily_dd_limit,
            )
            return True

        return False

    # -----------------------------------------------------------------
    # Position Sizing (now uses cached balance)
    # -----------------------------------------------------------------
    def calculate_position_size(
        self, entry_price: float, stop_price: float
    ) -> float:
        """Calculate position size in *base currency* units.

        Uses cached balance instead of a fresh API call – the kill-switch
        check earlier in the loop already refreshed the cache.
        """
        equity = self.get_cached_balance()
        risk_amount = equity * self.risk_per_trade  # e.g. 1 % of equity

        price_distance = abs(entry_price - stop_price)
        if price_distance == 0:
            logger.warning("Entry == Stop price; returning 0 size")
            return 0.0

        # position_size (base) = risk_amount / price_distance
        size = risk_amount / price_distance

        # Cap by leverage-adjusted equity
        max_notional = equity * self.leverage
        max_size = max_notional / entry_price
        size = min(size, max_size)

        logger.info(
            "Position size | equity={:.2f} | risk$={:.2f} | "
            "dist={:.2f} | size={:.6f}",
            equity,
            risk_amount,
            price_distance,
            size,
        )
        return size

    # -----------------------------------------------------------------
    # SL / TP
    # -----------------------------------------------------------------
    def get_stop_loss(self, entry_price: float, side: str, atr: float = 0.0) -> float:
        """SL with ATR or percentage + taker fee buffer."""
        buffer = 0.0008
        if cfg.SCALP_SL_USE_ATR and atr > 0:
            atr_distance = atr * cfg.SCALP_SL_ATR_MULTIPLIER
            if side.upper() == "BUY":
                sl = entry_price - atr_distance
            else:
                sl = entry_price + atr_distance
            logger.debug("ATR SL for {} @ {:.2f} -> {:.2f} (ATR={:.2f}, mult={:.1f})", 
                         side, entry_price, sl, atr, cfg.SCALP_SL_ATR_MULTIPLIER)
        else:
            if side.upper() == "BUY":
                sl = entry_price * (1 - self.stop_loss_pct - buffer)
            else:
                sl = entry_price * (1 + self.stop_loss_pct + buffer)
            logger.debug("SL for {} @ {:.2f} -> {:.2f} (buffer={:.4f})", side, entry_price, sl, buffer)
        return float(self.exchange.price_to_precision(self.symbol, sl))

    def get_take_profit(self, entry_price: float, side: str, atr: float = 0.0) -> float:
        """TP with ATR or percentage + taker fee buffer."""
        buffer = 0.0008
        if cfg.SCALP_SL_USE_ATR and atr > 0:
            atr_distance = atr * cfg.SCALP_TP_ATR_MULTIPLIER
            if side.upper() == "BUY":
                tp = entry_price + atr_distance
            else:
                tp = entry_price - atr_distance
            logger.debug("ATR TP for {} @ {:.2f} -> {:.2f} (ATR={:.2f}, mult={:.1f})", 
                         side, entry_price, tp, atr, cfg.SCALP_TP_ATR_MULTIPLIER)
        else:
            if side.upper() == "BUY":
                tp = entry_price * (1 + self.take_profit_pct - buffer)
            else:
                tp = entry_price * (1 - self.take_profit_pct - buffer)
            logger.debug("TP for {} @ {:.2f} -> {:.2f} (buffer={:.4f})", side, entry_price, tp, buffer)
        return float(self.exchange.price_to_precision(self.symbol, tp))

    # -----------------------------------------------------------------
    # Swing-specific SL / TP / Position Sizing
    # -----------------------------------------------------------------
    def get_swing_stop_loss(self, entry_price: float, side: str, symbol: str, atr: float = 0.0) -> float:
        """SL for swing trades. Uses ATR if enabled, else percentage."""
        if cfg.SWING_SL_USE_ATR and atr > 0:
            atr_sl_distance = atr * cfg.SWING_SL_ATR_MULTIPLIER
            if side.upper() == "BUY":
                sl = entry_price - atr_sl_distance
            else:
                sl = entry_price + atr_sl_distance
            logger.debug("[SWING] ATR-based SL for {} @ {:.2f} -> {:.2f} (ATR={:.2f}, mult={:.1f})", 
                         side, entry_price, sl, atr, cfg.SWING_SL_ATR_MULTIPLIER)
        else:
            buffer = 0.001
            if side.upper() == "BUY":
                sl = entry_price * (1 - cfg.SWING_STOP_LOSS_PCT - buffer)
            else:
                sl = entry_price * (1 + cfg.SWING_STOP_LOSS_PCT + buffer)
            logger.debug("[SWING] %-based SL for {} @ {:.2f} -> {:.2f}", side, entry_price, sl)
        return float(self.exchange.price_to_precision(symbol, sl))

    def get_swing_take_profit(self, entry_price: float, side: str, symbol: str) -> float:
        """TP for swing trades (wider than scalp)."""
        buffer = 0.001
        if side.upper() == "BUY":
            tp = entry_price * (1 + cfg.SWING_TAKE_PROFIT_PCT - buffer)
        else:
            tp = entry_price * (1 - cfg.SWING_TAKE_PROFIT_PCT - buffer)
        logger.debug("[SWING] TP for {} @ {:.2f} -> {:.2f}", side, entry_price, tp)
        return float(self.exchange.price_to_precision(symbol, tp))

    def get_swing_trailing_stop(self, entry_price: float, current_price: float, side: str, 
                                symbol: str, ema20: float = 0.0) -> float | None:
        """Calculate trailing stop for swing trades.
        
        Activates after price moves +SWING_TRAIL_ACTIVATE_PCT from entry.
        Returns trailing stop price, or None if trail not yet activated.
        """
        if side.upper() == "BUY":
            pnl_pct = (current_price - entry_price) / entry_price
            if pnl_pct < cfg.SWING_TRAIL_ACTIVATE_PCT:
                return None  # not activated yet
            # Trail by offset OR EMA20, whichever is tighter (higher for longs)
            trail_by_pct = current_price * (1 - cfg.SWING_TRAIL_OFFSET_PCT)
            trail_stop = trail_by_pct
            if cfg.SWING_TRAIL_USE_EMA20 and ema20 > 0:
                trail_stop = max(trail_by_pct, ema20)  # tighter = higher for longs
            logger.info("[SWING] Trailing stop activated for BUY @ {:.2f} -> trail={:.2f}", current_price, trail_stop)
            return float(self.exchange.price_to_precision(symbol, trail_stop))
        else:  # SELL
            pnl_pct = (entry_price - current_price) / entry_price
            if pnl_pct < cfg.SWING_TRAIL_ACTIVATE_PCT:
                return None
            trail_by_pct = current_price * (1 + cfg.SWING_TRAIL_OFFSET_PCT)
            trail_stop = trail_by_pct
            if cfg.SWING_TRAIL_USE_EMA20 and ema20 > 0:
                trail_stop = min(trail_by_pct, ema20)  # tighter = lower for shorts
            logger.info("[SWING] Trailing stop activated for SELL @ {:.2f} -> trail={:.2f}", current_price, trail_stop)
            return float(self.exchange.price_to_precision(symbol, trail_stop))

    def check_swing_total_exposure(self) -> bool:
        """Check if total swing exposure is below the cap (2-3% of equity)."""
        try:
            equity = self.get_cached_balance()
            positions = self.exchange.fetch_positions(cfg.SWING_SYMBOLS)
            total_notional = sum(
                abs(float(p.get("notional", 0))) for p in positions if float(p.get("contracts", 0)) > 0
            )
            exposure_pct = total_notional / equity if equity > 0 else 1.0
            under_cap = exposure_pct < cfg.SWING_MAX_TOTAL_EXPOSURE_PCT
            logger.debug("[SWING] Total exposure: {:.2%} / {:.2%} cap (ok={})", 
                         exposure_pct, cfg.SWING_MAX_TOTAL_EXPOSURE_PCT, under_cap)
            return under_cap
        except Exception as exc:
            logger.error("[SWING] Exposure check failed: {}", exc)
            return False

    def calculate_swing_position_size(self, entry_price: float, stop_price: float) -> float:
        """Position size for swing trades (uses SWING_RISK_PER_TRADE)."""
        equity = self.get_cached_balance()
        risk_amount = equity * cfg.SWING_RISK_PER_TRADE

        price_distance = abs(entry_price - stop_price)
        if price_distance == 0:
            logger.warning("[SWING] Entry == Stop; returning 0")
            return 0.0

        size = risk_amount / price_distance
        max_notional = equity * cfg.SWING_LEVERAGE
        max_size = max_notional / entry_price
        size = min(size, max_size)

        logger.info(
            "[SWING] Position size | equity={:.2f} | risk$={:.2f} | size={:.6f}",
            equity, risk_amount, size,
        )
        return size

    def check_swing_max_positions(self, symbols: list[str]) -> bool:
        """Check if swing positions are below the cap across all swing symbols.
        
        Also checks total exposure cap as an additional gate.
        """
        try:
            positions = self.exchange.fetch_positions(symbols)
            open_count = sum(
                1 for p in positions if float(p.get("contracts", 0)) > 0
            )
            under_limit = open_count < cfg.SWING_MAX_OPEN_POSITIONS
            logger.debug(
                "[SWING] Open positions: {} / {} (can_trade={})",
                open_count, cfg.SWING_MAX_OPEN_POSITIONS, under_limit,
            )
            if not under_limit:
                return False
            # Additional gate: check total exposure cap
            return self.check_swing_total_exposure()
        except Exception as exc:
            logger.error("[SWING] Failed to fetch positions: {}", exc)
            return False

    def check_swing_symbol_position(self, symbol: str) -> bool:
        """Check if we already have a swing position for this symbol."""
        try:
            positions = self.exchange.fetch_positions([symbol])
            for p in positions:
                if float(p.get("contracts", 0)) > 0:
                    return True
            return False
        except Exception as exc:
            logger.error("[SWING] Failed to check position for {}: {}", symbol, exc)
            return True  # block on error

    # -----------------------------------------------------------------
    # Position-Count Guard
    # -----------------------------------------------------------------
    def check_max_positions(self) -> bool:
        """Return *True* if the number of open positions is below the cap."""
        try:
            positions = self.exchange.fetch_positions([self.symbol])
            open_count = sum(
                1
                for p in positions
                if float(p.get("contracts", 0)) > 0
            )
            under_limit = open_count < self.max_positions
            logger.debug(
                "Open positions: {} / {} (can_trade={})",
                open_count,
                self.max_positions,
                under_limit,
            )
            return under_limit
        except Exception as exc:
            logger.error("Failed to fetch positions: {}", exc)
            return False  # block trading on error

    # -----------------------------------------------------------------
    # Daily Reset
    # -----------------------------------------------------------------
    def reset_daily(self) -> dict:
        """Reset daily stats at UTC midnight.  Returns a summary dict."""
        summary = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "start_balance": self.daily_start_balance,
            "end_balance": self._fetch_usdt_balance(),
            "pnl": self.daily_pnl,
            "trades": self.daily_trades,
            "wins": self.daily_wins,
            "win_rate": (
                self.daily_wins / self.daily_trades
                if self.daily_trades > 0
                else 0.0
            ),
            "kill_switch_triggered": self.kill_switch_active,
        }
        logger.info("Daily reset | summary: {}", summary)
        # Also reset trade tracker daily boundary
        if self.trade_tracker is not None:
            self.trade_tracker.reset_daily()
        self._sync_daily_balance()
        return summary

    # -----------------------------------------------------------------
    # Trade tracking helpers
    # -----------------------------------------------------------------
    def record_trade(self, is_win: bool) -> None:
        """Increment daily trade counters."""
        self.daily_trades += 1
        if is_win:
            self.daily_wins += 1
        logger.debug(
            "Trade recorded | wins={}/{}", self.daily_wins, self.daily_trades
        )

    async def record_trade_full(
        self,
        symbol: str,
        side: str,
        trade_type: str,
        entry_price: float,
        exit_price: float,
        size: float,
        reason: str,
        entry_time: str | None = None,
    ) -> dict | None:
        """Record trade in both daily counters and TradeTracker."""
        # Determine win/loss
        if side.lower() == "long":
            pnl = (exit_price - entry_price) * size
        else:
            pnl = (entry_price - exit_price) * size
        is_win = pnl > 0

        # Update daily counters
        self.record_trade(is_win)

        # Persist via TradeTracker if available
        if self.trade_tracker is not None:
            record = await self.trade_tracker.record_trade(
                symbol=symbol,
                side=side,
                trade_type=trade_type,
                entry_price=entry_price,
                exit_price=exit_price,
                size=size,
                reason=reason,
                entry_time=entry_time,
            )
            return record
        return None
