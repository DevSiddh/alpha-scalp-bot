"""Alpha-Scalp Bot – Premium Risk Management Engine.

Handles:
- 3% daily drawdown kill-switch
- Rolling daily P&L circuit breaker (pauses after X% daily loss)
- Position sizing (fixed fractional + Kelly Criterion)
- ATR-based trailing stop for scalp trades
- Stop-loss / take-profit calculation (ATR or percentage)
- Max concurrent trades limiter (scalp + swing combined)
- Max open-position enforcement per strategy
- Per-loop balance caching to reduce API calls
- Regime-aware SL/TP adjustments
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
    """Centralised premium risk-management layer."""

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

        # PREMIUM: Daily P&L circuit breaker
        self.daily_loss_limit: float = getattr(cfg, 'DAILY_LOSS_LIMIT', 0.03)
        self.daily_circuit_breaker_active: bool = False
        self.daily_realized_pnl: float = 0.0  # tracks realized P&L only

        # PREMIUM: Max concurrent trades (scalp + swing combined)
        self.max_concurrent_trades: int = getattr(cfg, 'MAX_CONCURRENT_TRADES', 3)

        # PREMIUM: ATR trailing stop state for scalp
        self._trailing_stops: dict[str, float] = {}  # order_id -> trailing_stop_price
        self._trailing_activated: dict[str, bool] = {}  # order_id -> activated
        self.trail_activate_pct: float = getattr(cfg, 'SCALP_TRAIL_ACTIVATE_PCT', 0.004)
        self.trail_atr_mult: float = getattr(cfg, 'SCALP_TRAIL_ATR_MULT', 1.0)

        # Trade tracker reference
        self.trade_tracker: Any | None = None

        # Balance cache
        self._cached_balance: float = 0.0
        self._cache_timestamp: float = 0.0
        self._cache_ttl: float = 3.0

        # Initialise
        self._sync_daily_balance()
        logger.info(
            "RiskEngine PREMIUM initialised | start_balance={:.2f} USDT | "
            "dd_limit={:.1%} | daily_loss_limit={:.1%} | risk/trade={:.1%} | "
            "leverage={}x | max_concurrent={}",
            self.daily_start_balance, self.daily_dd_limit,
            self.daily_loss_limit, self.risk_per_trade,
            self.leverage, self.max_concurrent_trades,
        )

    # -----------------------------------------------------------------
    # Trade Tracker integration
    # -----------------------------------------------------------------
    def set_trade_tracker(self, tracker: Any) -> None:
        self.trade_tracker = tracker
        logger.info("TradeTracker attached to RiskEngine")

    # -----------------------------------------------------------------
    # Balance helpers (with cache)
    # -----------------------------------------------------------------
    def _fetch_usdt_balance(self) -> float:
        try:
            bal = self.exchange.fetch_balance({"type": "future"})
            total = float(bal.get("total", {}).get("USDT", 0.0))
            self._cached_balance = total
            self._cache_timestamp = time.monotonic()
            logger.debug("Fetched USDT balance: {:.4f} (cache refreshed)", total)
            return total
        except Exception as exc:
            logger.error("Failed to fetch balance: {}", exc)
            raise

    def get_cached_balance(self) -> float:
        now = time.monotonic()
        if (now - self._cache_timestamp) < self._cache_ttl and self._cached_balance > 0:
            logger.debug("Using cached balance: {:.4f} (age={:.1f}s)",
                         self._cached_balance, now - self._cache_timestamp)
            return self._cached_balance
        return self._fetch_usdt_balance()

    def invalidate_balance_cache(self) -> None:
        self._cache_timestamp = 0.0
        logger.debug("Balance cache invalidated")

    def _sync_daily_balance(self) -> None:
        self.daily_start_balance = self._fetch_usdt_balance()
        self.daily_pnl = 0.0
        self.daily_realized_pnl = 0.0
        self.daily_trades = 0
        self.daily_wins = 0
        self.kill_switch_active = False
        self.daily_circuit_breaker_active = False
        logger.info("Daily balance synced: {:.2f} USDT", self.daily_start_balance)

    # -----------------------------------------------------------------
    # Kill Switch (3% daily drawdown)
    # -----------------------------------------------------------------
    def check_kill_switch(self) -> bool:
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

        logger.debug("Kill-switch check | start={:.2f} | now={:.2f} | dd={:.2%}",
                     self.daily_start_balance, current_balance, drawdown)

        if drawdown >= self.daily_dd_limit:
            self.kill_switch_active = True
            logger.critical("KILL SWITCH ACTIVATED | drawdown {:.2%} >= limit {:.2%}",
                            drawdown, self.daily_dd_limit)
            return True

        return False

    # -----------------------------------------------------------------
    # PREMIUM: Daily P&L Circuit Breaker
    # -----------------------------------------------------------------
    def record_trade_pnl(self, pnl: float, won: bool) -> None:
        """Record realized P&L from a closed trade. Updates circuit breaker."""
        self.daily_realized_pnl += pnl
        self.daily_trades += 1
        if won:
            self.daily_wins += 1

        # Check circuit breaker
        if self.daily_start_balance > 0:
            daily_loss_pct = -self.daily_realized_pnl / self.daily_start_balance
            if daily_loss_pct >= self.daily_loss_limit:
                self.daily_circuit_breaker_active = True
                logger.critical(
                    "CIRCUIT BREAKER ACTIVATED | daily realized loss {:.2%} >= {:.2%} limit | "
                    "P&L=${:.2f} | trades={} | Bot pausing new entries",
                    daily_loss_pct, self.daily_loss_limit,
                    self.daily_realized_pnl, self.daily_trades,
                )

        logger.info("Trade P&L recorded: ${:.2f} ({}) | daily total: ${:.2f} | trades: {}",
                    pnl, "WIN" if won else "LOSS", self.daily_realized_pnl, self.daily_trades)

    def check_circuit_breaker(self) -> bool:
        """Return True if daily circuit breaker is active (too many losses today)."""
        return self.daily_circuit_breaker_active

    # -----------------------------------------------------------------
    # PREMIUM: Concurrent Trade Limiter
    # -----------------------------------------------------------------
    def check_total_concurrent_trades(self) -> bool:
        """Return True if total open positions (scalp + swing) are below global cap."""
        try:
            # Fetch all positions for all symbols we trade
            all_symbols = [self.symbol]
            if getattr(cfg, 'SWING_ENABLED', False):
                all_symbols.extend(cfg.SWING_SYMBOLS)
            all_symbols = list(set(all_symbols))  # deduplicate

            positions = self.exchange.fetch_positions(all_symbols)
            open_count = sum(
                1 for p in positions if float(p.get("contracts", 0)) > 0
            )
            under_limit = open_count < self.max_concurrent_trades
            logger.debug(
                "Concurrent trades: {} / {} global cap (can_trade={})",
                open_count, self.max_concurrent_trades, under_limit,
            )
            if not under_limit:
                logger.warning(
                    "CONCURRENT LIMIT: {} open trades >= {} max – blocking new entry",
                    open_count, self.max_concurrent_trades,
                )
            return under_limit
        except Exception as exc:
            logger.error("Failed to check concurrent trades: {}", exc)
            return False

    # -----------------------------------------------------------------
    # Position Sizing (with Kelly override)
    # -----------------------------------------------------------------
    def calculate_position_size(
        self, entry_price: float, stop_price: float,
        kelly_fraction: float = 0.0,
    ) -> float:
        """Calculate position size in base currency units.
        
        If kelly_fraction > 0, uses Kelly-based risk instead of fixed.
        """
        equity = self.get_cached_balance()

        # Use Kelly if available, else fixed risk
        risk_pct = kelly_fraction if kelly_fraction > 0 else self.risk_per_trade
        risk_amount = equity * risk_pct

        price_distance = abs(entry_price - stop_price)
        if price_distance == 0:
            logger.warning("Entry == Stop price; returning 0 size")
            return 0.0

        size = risk_amount / price_distance

        # Cap by leverage-adjusted equity
        max_notional = equity * self.leverage
        max_size = max_notional / entry_price
        size = min(size, max_size)

        logger.info(
            "Position size | equity={:.2f} | risk_pct={:.2%} | risk$={:.2f} | "
            "dist={:.2f} | size={:.6f}{}",
            equity, risk_pct, risk_amount, price_distance, size,
            " (Kelly)" if kelly_fraction > 0 else "",
        )
        return size

    # -----------------------------------------------------------------
    # SL / TP (with regime adjustment)
    # -----------------------------------------------------------------
    def get_stop_loss(self, entry_price: float, side: str, atr: float = 0.0,
                      regime: str = "RANGING") -> float:
        """SL with ATR or percentage + taker fee buffer.
        
        Regime adjustment:
        - TRENDING/VOLATILE: widen SL by 20% (let trends breathe)
        - RANGING: standard SL
        """
        buffer = 0.0008
        regime_mult = 1.2 if regime in ("TRENDING", "VOLATILE") else 1.0

        if cfg.SCALP_SL_USE_ATR and atr > 0:
            atr_distance = atr * cfg.SCALP_SL_ATR_MULTIPLIER * regime_mult
            if side.upper() == "BUY":
                sl = entry_price - atr_distance
            else:
                sl = entry_price + atr_distance
            logger.debug("ATR SL for {} @ {:.2f} -> {:.2f} (ATR={:.2f}, mult={:.1f}, regime={})",
                         side, entry_price, sl, atr, cfg.SCALP_SL_ATR_MULTIPLIER, regime)
        else:
            effective_sl = self.stop_loss_pct * regime_mult
            if side.upper() == "BUY":
                sl = entry_price * (1 - effective_sl - buffer)
            else:
                sl = entry_price * (1 + effective_sl + buffer)
            logger.debug("SL for {} @ {:.2f} -> {:.2f} (regime={})", side, entry_price, sl, regime)
        return float(self.exchange.price_to_precision(self.symbol, sl))

    def get_take_profit(self, entry_price: float, side: str, atr: float = 0.0,
                        regime: str = "RANGING") -> float:
        """TP with ATR or percentage + taker fee buffer.
        
        Regime adjustment:
        - TRENDING/VOLATILE: widen TP by 30% (let profits run)
        - RANGING: standard TP
        """
        buffer = 0.0008
        regime_mult = 1.3 if regime in ("TRENDING", "VOLATILE") else 1.0

        if cfg.SCALP_SL_USE_ATR and atr > 0:
            atr_distance = atr * cfg.SCALP_TP_ATR_MULTIPLIER * regime_mult
            if side.upper() == "BUY":
                tp = entry_price + atr_distance
            else:
                tp = entry_price - atr_distance
            logger.debug("ATR TP for {} @ {:.2f} -> {:.2f} (ATR={:.2f}, mult={:.1f}, regime={})",
                         side, entry_price, tp, atr, cfg.SCALP_TP_ATR_MULTIPLIER, regime)
        else:
            effective_tp = self.take_profit_pct * regime_mult
            if side.upper() == "BUY":
                tp = entry_price * (1 + effective_tp - buffer)
            else:
                tp = entry_price * (1 - effective_tp - buffer)
            logger.debug("TP for {} @ {:.2f} -> {:.2f} (regime={})", side, entry_price, tp, regime)
        return float(self.exchange.price_to_precision(self.symbol, tp))

    # -----------------------------------------------------------------
    # PREMIUM: ATR Trailing Stop for Scalp Trades
    # -----------------------------------------------------------------
    def init_trailing_stop(self, order_id: str, entry_price: float, side: str,
                           atr: float) -> None:
        """Initialize trailing stop tracking for a new scalp position."""
        if side.upper() == "BUY":
            initial_trail = entry_price - (atr * self.trail_atr_mult)
        else:
            initial_trail = entry_price + (atr * self.trail_atr_mult)

        self._trailing_stops[order_id] = initial_trail
        self._trailing_activated[order_id] = False
        logger.info("Trailing stop initialized for {} | entry={:.2f} | "
                    "initial_trail={:.2f} | ATR={:.2f}",
                    order_id, entry_price, initial_trail, atr)

    def update_trailing_stop(self, order_id: str, current_price: float,
                             entry_price: float, side: str, atr: float) -> float | None:
        """Update and return trailing stop price. Returns None if not yet activated.
        
        Activation: price moves +trail_activate_pct from entry.
        Once active: trail = current_price - (ATR * trail_atr_mult) for longs.
        Trail only moves in favorable direction (ratchets up for longs, down for shorts).
        """
        if order_id not in self._trailing_stops:
            return None

        # Check activation
        if not self._trailing_activated.get(order_id, False):
            if side.upper() == "BUY":
                pnl_pct = (current_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - current_price) / entry_price

            if pnl_pct >= self.trail_activate_pct:
                self._trailing_activated[order_id] = True
                logger.info("TRAILING STOP ACTIVATED for {} | pnl={:.2%} >= {:.2%}",
                            order_id, pnl_pct, self.trail_activate_pct)
            else:
                return None  # not activated yet

        # Calculate new trailing stop
        atr_trail = atr * self.trail_atr_mult
        old_trail = self._trailing_stops[order_id]

        if side.upper() == "BUY":
            new_trail = current_price - atr_trail
            # Only ratchet UP for longs
            if new_trail > old_trail:
                self._trailing_stops[order_id] = new_trail
                logger.debug("Trail UP for {}: {:.2f} -> {:.2f} (price={:.2f})",
                             order_id, old_trail, new_trail, current_price)
            return self._trailing_stops[order_id]
        else:
            new_trail = current_price + atr_trail
            # Only ratchet DOWN for shorts
            if new_trail < old_trail:
                self._trailing_stops[order_id] = new_trail
                logger.debug("Trail DOWN for {}: {:.2f} -> {:.2f} (price={:.2f})",
                             order_id, old_trail, new_trail, current_price)
            return self._trailing_stops[order_id]

    def check_trailing_stop_hit(self, order_id: str, current_price: float,
                                side: str) -> bool:
        """Check if current price has hit the trailing stop."""
        if order_id not in self._trailing_stops:
            return False
        if not self._trailing_activated.get(order_id, False):
            return False

        trail = self._trailing_stops[order_id]
        if side.upper() == "BUY":
            hit = current_price <= trail
        else:
            hit = current_price >= trail

        if hit:
            logger.info("TRAILING STOP HIT for {} | price={:.2f} {} trail={:.2f}",
                        order_id, current_price, "<=" if side.upper() == "BUY" else ">=", trail)
        return hit

    def remove_trailing_stop(self, order_id: str) -> None:
        """Clean up trailing stop state for a closed position."""
        self._trailing_stops.pop(order_id, None)
        self._trailing_activated.pop(order_id, None)

    # -----------------------------------------------------------------
    # Swing-specific SL / TP / Position Sizing
    # -----------------------------------------------------------------
    def get_swing_stop_loss(self, entry_price: float, side: str, symbol: str, atr: float = 0.0) -> float:
        if cfg.SWING_SL_USE_ATR and atr > 0:
            atr_sl_distance = atr * cfg.SWING_SL_ATR_MULTIPLIER
            if side.upper() == "BUY":
                sl = entry_price - atr_sl_distance
            else:
                sl = entry_price + atr_sl_distance
            logger.debug("[SWING] ATR-based SL for {} @ {:.2f} -> {:.2f}", side, entry_price, sl)
        else:
            buffer = 0.001
            if side.upper() == "BUY":
                sl = entry_price * (1 - cfg.SWING_STOP_LOSS_PCT - buffer)
            else:
                sl = entry_price * (1 + cfg.SWING_STOP_LOSS_PCT + buffer)
            logger.debug("[SWING] %-based SL for {} @ {:.2f} -> {:.2f}", side, entry_price, sl)
        return float(self.exchange.price_to_precision(symbol, sl))

    def get_swing_take_profit(self, entry_price: float, side: str, symbol: str) -> float:
        buffer = 0.001
        if side.upper() == "BUY":
            tp = entry_price * (1 + cfg.SWING_TAKE_PROFIT_PCT - buffer)
        else:
            tp = entry_price * (1 - cfg.SWING_TAKE_PROFIT_PCT - buffer)
        logger.debug("[SWING] TP for {} @ {:.2f} -> {:.2f}", side, entry_price, tp)
        return float(self.exchange.price_to_precision(symbol, tp))

    def get_swing_trailing_stop(self, entry_price: float, current_price: float, side: str,
                                symbol: str, ema20: float = 0.0) -> float | None:
        if side.upper() == "BUY":
            pnl_pct = (current_price - entry_price) / entry_price
            if pnl_pct < cfg.SWING_TRAIL_ACTIVATE_PCT:
                return None
            trail_by_pct = current_price * (1 - cfg.SWING_TRAIL_OFFSET_PCT)
            trail_stop = trail_by_pct
            if cfg.SWING_TRAIL_USE_EMA20 and ema20 > 0:
                trail_stop = max(trail_by_pct, ema20)
            logger.info("[SWING] Trailing stop for BUY @ {:.2f} -> trail={:.2f}", current_price, trail_stop)
            return float(self.exchange.price_to_precision(symbol, trail_stop))
        else:
            pnl_pct = (entry_price - current_price) / entry_price
            if pnl_pct < cfg.SWING_TRAIL_ACTIVATE_PCT:
                return None
            trail_by_pct = current_price * (1 + cfg.SWING_TRAIL_OFFSET_PCT)
            trail_stop = trail_by_pct
            if cfg.SWING_TRAIL_USE_EMA20 and ema20 > 0:
                trail_stop = min(trail_by_pct, ema20)
            logger.info("[SWING] Trailing stop for SELL @ {:.2f} -> trail={:.2f}", current_price, trail_stop)
            return float(self.exchange.price_to_precision(symbol, trail_stop))

    def check_swing_total_exposure(self) -> bool:
        try:
            equity = self.get_cached_balance()
            positions = self.exchange.fetch_positions(cfg.SWING_SYMBOLS)
            total_notional = sum(
                abs(float(p.get("notional", 0))) for p in positions if float(p.get("contracts", 0)) > 0
            )
            exposure_pct = total_notional / equity if equity > 0 else 1.0
            under_cap = exposure_pct < cfg.SWING_MAX_TOTAL_EXPOSURE_PCT
            logger.debug("[SWING] Total exposure: {:.2%} / {:.2%} cap",
                         exposure_pct, cfg.SWING_MAX_TOTAL_EXPOSURE_PCT)
            return under_cap
        except Exception as exc:
            logger.error("[SWING] Exposure check failed: {}", exc)
            return False

    def calculate_swing_position_size(self, entry_price: float, stop_price: float) -> float:
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
        logger.info("[SWING] Position size | equity={:.2f} | risk$={:.2f} | size={:.6f}",
                    equity, risk_amount, size)
        return size

    def check_swing_max_positions(self, symbols: list[str]) -> bool:
        try:
            positions = self.exchange.fetch_positions(symbols)
            open_count = sum(1 for p in positions if float(p.get("contracts", 0)) > 0)
            under_limit = open_count < cfg.SWING_MAX_OPEN_POSITIONS
            logger.debug("[SWING] Open positions: {} / {}", open_count, cfg.SWING_MAX_OPEN_POSITIONS)
            if not under_limit:
                return False
            return self.check_swing_total_exposure()
        except Exception as exc:
            logger.error("[SWING] Failed to fetch positions: {}", exc)
            return False

    def check_swing_symbol_position(self, symbol: str) -> bool:
        try:
            positions = self.exchange.fetch_positions([symbol])
            for p in positions:
                if float(p.get("contracts", 0)) > 0:
                    return True
            return False
        except Exception as exc:
            logger.error("[SWING] Failed to check position for {}: {}", symbol, exc)
            return True

    # -----------------------------------------------------------------
    # Position-Count Guard (scalp only)
    # -----------------------------------------------------------------
    def check_max_positions(self) -> bool:
        try:
            positions = self.exchange.fetch_positions([self.symbol])
            open_count = sum(1 for p in positions if float(p.get("contracts", 0)) > 0)
            under_limit = open_count < self.max_positions
            logger.debug("Open positions: {} / {} (can_trade={})",
                         open_count, self.max_positions, under_limit)
            return under_limit
        except Exception as exc:
            logger.error("Failed to fetch positions: {}", exc)
            return False

    # -----------------------------------------------------------------
    # PREMIUM: Pre-trade gate (all checks combined)
    # -----------------------------------------------------------------
    def can_open_trade(self) -> tuple[bool, str]:
        """Run ALL premium risk checks before opening a new trade.
        
        Returns (allowed, reason).
        """
        # 1. Kill switch
        if self.check_kill_switch():
            return False, "Kill switch active (daily drawdown limit hit)"

        # 2. Circuit breaker
        if self.check_circuit_breaker():
            return False, f"Circuit breaker active (daily loss >= {self.daily_loss_limit:.1%})"

        # 3. Scalp position limit
        if not self.check_max_positions():
            return False, f"Max scalp positions ({self.max_positions}) reached"

        # 4. Global concurrent trade limit
        if not self.check_total_concurrent_trades():
            return False, f"Max concurrent trades ({self.max_concurrent_trades}) reached"

        return True, "All checks passed"

    # -----------------------------------------------------------------
    # Daily Reset
    # -----------------------------------------------------------------
    def reset_daily(self) -> dict:
        summary = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "start_balance": self.daily_start_balance,
            "end_balance": self._fetch_usdt_balance(),
            "pnl": self.daily_pnl,
            "realized_pnl": self.daily_realized_pnl,
            "trades": self.daily_trades,
            "wins": self.daily_wins,
            "win_rate": (self.daily_wins / self.daily_trades if self.daily_trades > 0 else 0.0),
            "kill_switch_triggered": self.kill_switch_active,
            "circuit_breaker_triggered": self.daily_circuit_breaker_active,
        }
        logger.info("Daily reset | summary: {}", summary)
        if self.trade_tracker is not None:
            self.trade_tracker.reset_daily()
        # Clear trailing stop state
        self._trailing_stops.clear()
        self._trailing_activated.clear()
        self._sync_daily_balance()
        return summary
