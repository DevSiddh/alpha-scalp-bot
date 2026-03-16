"""Alpha-Scalp Bot – Premium Risk Management Engine.

Handles:
- 3% daily drawdown kill-switch
- Rolling daily P&L circuit breaker (pauses after X% daily loss)
- Three-Strike cooldown (3 consecutive losses → 90-min pause)
- Equity Floor shutdown (balance < 80% of session start → halt)
- Active Cash Mode (balance 80-90% of start → 50% position size)
- Position sizing (fixed fractional + Kelly Criterion, min 300 trades)
- ATR validation (reject zero/sub-threshold ATR)
- Minimum SL floor (0.15% of entry)
- Regime-aware R:R enforcement (RANGING=1.5, TRENDING=2.0, VOLATILE=1.8)
- ATR-based trailing stop for scalp trades
- Stop-loss / take-profit calculation (ATR or percentage)
- Max concurrent trades limiter (scalp + swing combined)
- Max open-position enforcement per strategy
- Per-loop balance caching to reduce API calls
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger

import config as cfg

if TYPE_CHECKING:
    import ccxt


@dataclass
class RiskDecision:
    """Result of a risk check gate."""
    allowed: bool
    reason: str | None = None


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
        self.stop_loss_pct: float = cfg.TOKEN_SL_PCT
        self.take_profit_pct: float = cfg.TOKEN_TP_PCT
        self.max_positions: int = cfg.MAX_OPEN_POSITIONS
        self.leverage: int = cfg.TOKEN_LEVERAGE
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

        # GP-S2: Three-Strike cooldown (3 consecutive losses → 90-min pause)
        self._consecutive_losses: int = 0
        self._three_strike_cooldown_until: float = 0.0
        self._three_strike_losses_required: int = getattr(cfg, 'THREE_STRIKE_LOSSES', 3)
        self._three_strike_cooldown_seconds: float = getattr(cfg, 'THREE_STRIKE_COOLDOWN_SECONDS', 5400.0)

        # GP-S2: Equity Floor (80% of session start → permanent halt)
        self.equity_floor_active: bool = False
        self._equity_floor_pct: float = getattr(cfg, 'EQUITY_FLOOR_PCT', 0.80)

        # GP-S2: Active Cash Mode (< 90% equity → 50% position size)
        self._active_cash_threshold_pct: float = getattr(cfg, 'ACTIVE_CASH_THRESHOLD_PCT', 0.90)

        # GP-S2: Minimum SL floor (0.15% of entry price)
        self._min_sl_floor_pct: float = getattr(cfg, 'MIN_SL_FLOOR_PCT', 0.0015)

        # GP-S2: ATR minimum threshold (0.05% of entry price)
        self._atr_min_pct: float = getattr(cfg, 'ATR_MIN_PCT', 0.0005)

        # GP-S2: Regime-aware minimum R:R
        self._regime_min_rr: dict[str, float] = getattr(cfg, 'REGIME_MIN_RR', {
            "RANGING": 1.5, "NEUTRAL": 1.5,
            "TRENDING": 2.0, "TRENDING_UP": 2.0, "TRENDING_DOWN": 2.0,
            "VOLATILE": 1.8,
        })

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
        if getattr(cfg, "PAPER_TRADING_MODE", False):
            import time
            bal = getattr(cfg, "INITIAL_BALANCE", 10000.0) + self.daily_realized_pnl
            self._cached_balance = bal
            self._cache_timestamp = time.monotonic()
            return bal
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

    def get_effective_leverage(self) -> int:
        """Return leverage scaled down based on daily drawdown (P1-9)."""
        base = self.leverage
        dd = self._current_drawdown_pct()
        if dd >= 0.05:
            return max(1, int(base * 0.25))
        if dd >= 0.035:
            return max(1, int(base * 0.25))
        if dd >= 0.02:
            return max(1, int(base * 0.5))
        return base

    def _current_drawdown_pct(self) -> float:
        """Current daily drawdown as a positive fraction."""
        if self.daily_start_balance <= 0:
            return 0.0
        loss = self.daily_start_balance - (self.daily_start_balance + self.daily_realized_pnl)
        return max(0.0, loss / self.daily_start_balance)

    def _sync_daily_balance(self) -> None:
        try:
            self.daily_start_balance = self._fetch_usdt_balance()
        except Exception:
            # Demo/testnet API may fail — use a safe default so bot can start
            fallback = getattr(cfg, 'INITIAL_BALANCE', 10000.0)
            logger.warning(
                "Balance fetch failed at startup — using fallback {:.2f} USDT",
                fallback,
            )
            self.daily_start_balance = fallback
            self._cached_balance = fallback
            self._cache_timestamp = time.monotonic()
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
            # Fallback to last known cached balance instead of killing the bot
            if self._cached_balance > 0:
                logger.warning(
                    "Balance read failed – using cached balance {:.2f} USDT",
                    self._cached_balance,
                )
                current_balance = self._cached_balance
            elif self.daily_start_balance > 0:
                logger.warning(
                    "Balance read failed – using daily start balance {:.2f} USDT",
                    self.daily_start_balance,
                )
                current_balance = self.daily_start_balance
            else:
                logger.warning("Balance read failed with no cached fallback – activating kill switch")
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
        """Record realized P&L from a closed trade. Updates circuit breaker + three-strike."""
        self.daily_realized_pnl += pnl
        self.daily_trades += 1
        if won:
            self.daily_wins += 1
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._three_strike_losses_required:
                self._three_strike_cooldown_until = time.time() + self._three_strike_cooldown_seconds
                self._consecutive_losses = 0  # reset counter after triggering
                logger.critical(
                    "THREE-STRIKE COOLDOWN ACTIVATED | {} consecutive losses | "
                    "cooling down {:.0f}s ({:.0f}min)",
                    self._three_strike_losses_required,
                    self._three_strike_cooldown_seconds,
                    self._three_strike_cooldown_seconds / 60,
                )

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

        logger.info("Trade P&L recorded: ${:.2f} ({}) | daily total: ${:.2f} | trades={} | streak={}",
            pnl, "WIN" if won else "LOSS", self.daily_realized_pnl, self.daily_trades,
            f"-{self._consecutive_losses}" if not won else "reset")

    def check_circuit_breaker(self) -> bool:
        """Return True if daily circuit breaker is active (too many losses today)."""
        return self.daily_circuit_breaker_active

    # -----------------------------------------------------------------
    # GP-S2: Three-Strike Cooldown
    # -----------------------------------------------------------------
    def check_three_strike_cooldown(self) -> bool:
        """Return True if we are inside a three-strike cooling-off window."""
        if time.time() < self._three_strike_cooldown_until:
            remaining = self._three_strike_cooldown_until - time.time()
            logger.warning(
                "THREE-STRIKE COOLDOWN active | {:.0f}s ({:.1f}min) remaining",
                remaining, remaining / 60,
            )
            return True
        return False

    # -----------------------------------------------------------------
    # GP-S2: Equity Floor & Active Cash Mode
    # -----------------------------------------------------------------
    def check_equity_floor(self) -> bool:
        """Return True (halt) if balance has fallen to or below the equity floor (80%)."""
        if self.equity_floor_active:
            return True
        try:
            balance = self.get_cached_balance()
        except Exception:
            return False
        if self.daily_start_balance <= 0:
            return False
        ratio = balance / self.daily_start_balance
        if ratio <= self._equity_floor_pct:
            self.equity_floor_active = True
            logger.critical(
                "EQUITY FLOOR HIT | balance={:.2f} = {:.1%} of start={:.2f} (floor={:.0%})",
                balance, ratio, self.daily_start_balance, self._equity_floor_pct,
            )
            return True
        return False

    def get_active_cash_multiplier(self) -> float:
        """Return 0.5 if equity is in the Active Cash zone (80-90% of start), else 1.0."""
        try:
            balance = self.get_cached_balance()
        except Exception:
            return 1.0
        if self.daily_start_balance <= 0:
            return 1.0
        ratio = balance / self.daily_start_balance
        if self._equity_floor_pct < ratio < self._active_cash_threshold_pct:
            logger.info(
                "ACTIVE CASH MODE | equity={:.1%} of start → 50% position size",
                ratio,
            )
            return 0.5
        return 1.0

    # -----------------------------------------------------------------
    # GP-S3: Spread Guard (standalone, callable before order placement)
    # -----------------------------------------------------------------
    def check_spread_guard(self, ask: float, bid: float) -> tuple[bool, str]:
        """GP-S3: Return (True, 'ok') if bid-ask spread is within MAX_SPREAD_BPS.

        Separates the spread check from sanity_guard() so callers can
        gate on spread alone without needing all sanity_guard inputs.
        """
        if ask <= 0 or bid <= 0:
            logger.warning("Spread guard: invalid prices ask={} bid={}", ask, bid)
            return False, "invalid_prices"
        mid = (ask + bid) / 2
        if mid <= 0:
            return False, "invalid_mid"
        spread_bps = ((ask - bid) / mid) * 10_000
        max_bps = getattr(cfg, 'MAX_SPREAD_BPS', 20)
        if spread_bps > max_bps:
            logger.warning(
                "Spread guard blocked | {:.1f}bps > {:.0f}bps limit | ask={} bid={}",
                spread_bps, max_bps, ask, bid,
            )
            return False, f"spread_too_wide_{spread_bps:.1f}bps"
        logger.debug("Spread guard OK | {:.1f}bps <= {:.0f}bps", spread_bps, max_bps)
        return True, "ok"

    # -----------------------------------------------------------------
    # GP-S2: ATR Validation
    # -----------------------------------------------------------------
    def validate_atr(self, atr: float, entry_price: float) -> bool:
        """Return False if ATR is zero or suspiciously low (< 0.05% of entry)."""
        if atr <= 0:
            logger.warning("ATR validation failed | atr={:.6f} (must be > 0)", atr)
            return False
        if entry_price > 0 and (atr / entry_price) < self._atr_min_pct:
            logger.warning(
                "ATR validation failed | atr={:.4f} = {:.4%} of price < min {:.4%}",
                atr, atr / entry_price, self._atr_min_pct,
            )
            return False
        return True

    # -----------------------------------------------------------------
    # PREMIUM: Concurrent Trade Limiter
    # -----------------------------------------------------------------
    def check_total_concurrent_trades(self) -> bool:
        """Return True if total open positions (scalp + swing) are below global cap."""
        try:
            # Fetch all positions for all symbols we trade
            if getattr(cfg, "PAPER_TRADING_MODE", False): return True
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

        If kelly_fraction > 0, applies Kelly with warm-up safety:
        - Below KELLY_MIN_TRADES: ignore Kelly, use fixed risk
        - Between MIN and RAMP trades: linearly blend fixed -> Kelly
        - Above RAMP trades: full Kelly (capped at KELLY_MAX_FRACTION)
        """
        equity = self.get_cached_balance()

        # Kelly warm-up: don't trust small sample sizes
        risk_pct = self.risk_per_trade  # default: fixed fractional
        if kelly_fraction > 0:
            # Hard cap: never exceed KELLY_MAX_FRACTION regardless of sample
            capped_kelly = min(kelly_fraction, cfg.KELLY_MAX_FRACTION)

            # Count completed trades from tracker
            n_trades = 0
            if self.trade_tracker is not None:
                n_trades = len(getattr(self.trade_tracker, '_trades', []))

            if n_trades >= cfg.KELLY_RAMP_TRADES:
                # Fully warmed up: use capped Kelly
                risk_pct = capped_kelly
                logger.debug("Kelly FULL | n={} | kelly={:.3%} -> capped={:.3%}",
                    n_trades, kelly_fraction, capped_kelly)
            elif n_trades >= cfg.KELLY_MIN_TRADES:
                # Ramp zone: linearly blend fixed -> Kelly
                ramp_progress = (n_trades - cfg.KELLY_MIN_TRADES) / (
                    cfg.KELLY_RAMP_TRADES - cfg.KELLY_MIN_TRADES
                )
                risk_pct = self.risk_per_trade + ramp_progress * (
                    capped_kelly - self.risk_per_trade
                )
                logger.debug("Kelly RAMP | n={} | blend={:.1%} | risk={:.3%}",
                    n_trades, ramp_progress, risk_pct)
            else:
                # Too few trades: ignore Kelly entirely
                logger.debug("Kelly SKIP | n={} < min={} | using fixed={:.3%}",
                    n_trades, cfg.KELLY_MIN_TRADES, self.risk_per_trade)

        # GP-S2: Active Cash Mode — halve size when equity is 80-90% of start
        cash_mult = self.get_active_cash_multiplier()
        risk_amount = equity * risk_pct * cash_mult

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
            "Position size | equity={:.2f} | risk_pct={:.2%} | cash_mult={:.1f} | "
            "risk$={:.2f} | dist={:.2f} | size={:.6f}{}",
            equity, risk_pct, cash_mult, risk_amount, price_distance, size,
            " (Kelly)" if kelly_fraction > 0 else "",
        )
        return size

    # -----------------------------------------------------------------
    # SL / TP (with regime adjustment + ATR-based dynamic P1-2)
    # -----------------------------------------------------------------
    def get_stop_loss(self, entry_price: float, side: str, atr: float = 0.0,
                      regime: str = "RANGING") -> float:
        """SL with ATR or percentage + taker fee buffer.

        P1-2: If atr > 0, uses ATR-based SL with R:R check.
        GP-S2: Applies regime-aware min R:R and 0.15% SL floor.

        Regime adjustment:
        - TRENDING/VOLATILE: widen SL by 20% (let trends breathe)
        - RANGING: standard SL
        """
        buffer = 0.0008
        regime_mult = 1.2 if regime in ("TRENDING", "VOLATILE", "TRENDING_UP", "TRENDING_DOWN") else 1.0
        # GP-S2: regime-aware minimum R:R
        min_rr_ratio = self._regime_min_rr.get(regime, getattr(cfg, 'MIN_REWARD_RISK_RATIO', 1.8))

        # P1-2: ATR-based dynamic SL with R:R check
        if atr > 0:
            atr_sl_mult = getattr(cfg, 'ATR_SL_MULTIPLIER', 1.5)
            atr_tp_mult = getattr(cfg, 'ATR_TP_MULTIPLIER', 3.0)

            sl_distance = atr * atr_sl_mult
            tp_distance = atr * atr_tp_mult

            sl_pct = sl_distance / entry_price
            tp_pct = tp_distance / entry_price

            # R:R check - if ratio < minimum, use TOKEN_PROFILES fallback
            if sl_pct > 0:
                rr_ratio = tp_pct / sl_pct
                if rr_ratio >= min_rr_ratio:
                    # Use ATR-based SL
                    if side.upper() == "BUY":
                        sl = entry_price - sl_distance * regime_mult
                    else:
                        sl = entry_price + sl_distance * regime_mult
                    sl = self._apply_sl_floor(sl, entry_price, side)
                    logger.debug(
                        "ATR SL for {} @ {:.2f} -> {:.2f} (ATR={:.2f}, mult={:.1f}, R:R={:.2f}, regime={})",
                        side, entry_price, sl, atr, atr_sl_mult, rr_ratio, regime,
                    )
                    return float(self.exchange.price_to_precision(self.symbol, sl))

        # Fallback to TOKEN_PROFILES or percentage
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

        sl = self._apply_sl_floor(sl, entry_price, side)
        return float(self.exchange.price_to_precision(self.symbol, sl))

    def _apply_sl_floor(self, sl: float, entry_price: float, side: str) -> float:
        """GP-S2: Enforce minimum SL distance of 0.15% from entry."""
        min_distance = entry_price * self._min_sl_floor_pct
        actual_distance = abs(entry_price - sl)
        if actual_distance < min_distance:
            if side.upper() == "BUY":
                sl = entry_price - min_distance
            else:
                sl = entry_price + min_distance
            logger.warning(
                "SL floored to min {:.2%} | new_sl={:.2f} (was {:.4f} away, min {:.4f})",
                self._min_sl_floor_pct, sl, actual_distance, min_distance,
            )
        return sl

    def get_take_profit(self, entry_price: float, side: str, atr: float = 0.0,
                        regime: str = "RANGING") -> float:
        """TP with ATR or percentage + taker fee buffer.

        P1-2: If atr > 0, uses ATR-based TP with R:R check.
        GP-S2: Uses regime-aware minimum R:R.

        Regime adjustment:
        - TRENDING/VOLATILE: widen TP by 30% (let profits run)
        - RANGING: standard TP
        """
        buffer = 0.0008
        regime_mult = 1.3 if regime in ("TRENDING", "VOLATILE", "TRENDING_UP", "TRENDING_DOWN") else 1.0
        # GP-S2: regime-aware minimum R:R
        min_rr_ratio = self._regime_min_rr.get(regime, getattr(cfg, 'MIN_REWARD_RISK_RATIO', 1.8))

        # P1-2: ATR-based dynamic TP with R:R check
        if atr > 0:
            atr_sl_mult = getattr(cfg, 'ATR_SL_MULTIPLIER', 1.5)
            atr_tp_mult = getattr(cfg, 'ATR_TP_MULTIPLIER', 3.0)

            sl_distance = atr * atr_sl_mult
            tp_distance = atr * atr_tp_mult

            sl_pct = sl_distance / entry_price
            tp_pct = tp_distance / entry_price

            # R:R check - if ratio < minimum, use TOKEN_PROFILES fallback
            if sl_pct > 0:
                rr_ratio = tp_pct / sl_pct
                if rr_ratio >= min_rr_ratio:
                    # Use ATR-based TP
                    if side.upper() == "BUY":
                        tp = entry_price + tp_distance * regime_mult
                    else:
                        tp = entry_price - tp_distance * regime_mult
                    logger.debug(
                        "ATR TP for {} @ {:.2f} -> {:.2f} (ATR={:.2f}, mult={:.1f}, R:R={:.2f}, regime={})",
                        side, entry_price, tp, atr, atr_tp_mult, rr_ratio, regime,
                    )
                    return float(self.exchange.price_to_precision(self.symbol, tp))

        # Fallback to TOKEN_PROFILES or percentage
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
            if getattr(cfg, "PAPER_TRADING_MODE", False): return True
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
            if getattr(cfg, "PAPER_TRADING_MODE", False): return True
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
            if getattr(cfg, "PAPER_TRADING_MODE", False): return False
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
            if getattr(cfg, "PAPER_TRADING_MODE", False): return True
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
    def sanity_guard(self, entry: float, vwap: float, ask: float, bid: float, last_trade_time: float, requested_size: float) -> tuple[bool, str, float]:
        if vwap > 0 and abs(entry - vwap) / vwap > 0.01:
            return False, "vwap_deviation_too_high", requested_size
        mid = (ask + bid) / 2
        if mid > 0 and (ask - bid) / mid > getattr(cfg, 'MAX_SPREAD_BPS', 20) / 10000.0:
            return False, "spread_too_wide", requested_size
        if time.time() - last_trade_time > getattr(cfg, 'DATA_FRESHNESS_SECONDS', 5):
            return False, "stale_data", requested_size
        
        # Clamp size
        notional = requested_size * entry
        min_pos = getattr(cfg, 'MIN_POSITION_SIZE_USDT', 6)
        max_pos = getattr(cfg, 'MAX_POSITION_SIZE_USDT', 15)
        
        if notional < min_pos:
            notional = min_pos
        elif notional > max_pos:
            notional = max_pos
            
        clamped_size = notional / entry
        return True, "ok", clamped_size

    def can_open_trade(self) -> tuple[bool, str]:
        """Run ALL premium risk checks before opening a new trade.

        Returns (allowed, reason).
        """
        # 1. Kill switch
        if self.check_kill_switch():
            return False, "Kill switch active (daily drawdown limit hit)"

        # 2. GP-S2: Equity Floor (permanent halt)
        if self.check_equity_floor():
            return False, f"Equity floor hit (balance <= {self._equity_floor_pct:.0%} of start)"

        # 3. Circuit breaker
        if self.check_circuit_breaker():
            return False, f"Circuit breaker active (daily loss >= {self.daily_loss_limit:.1%})"

        # 4. GP-S2: Three-Strike cooldown
        if self.check_three_strike_cooldown():
            remaining = max(0, self._three_strike_cooldown_until - time.time())
            return False, f"Three-strike cooldown active ({remaining:.0f}s remaining)"

        # 5. Scalp position limit
        if not self.check_max_positions():
            return False, f"Max scalp positions ({self.max_positions}) reached"

        # 6. Global concurrent trade limit
        if not self.check_total_concurrent_trades():
            return False, f"Max concurrent trades ({self.max_concurrent_trades}) reached"

        return True, "All checks passed"

    # -----------------------------------------------------------------
    # ATR-based SL/TP Calculation (P1-2)
    # -----------------------------------------------------------------
    def calculate_atr_based_sl_tp(
        self,
        entry_price: float,
        atr: float,
        is_long: bool = True,
    ) -> tuple[float, float]:
        """Calculate stop-loss and take-profit prices using ATR multiples.

        Args:
            entry_price: The entry price for the trade.
            atr: Current ATR value.
            is_long: True for long position, False for short.

        Returns:
            Tuple of (sl_price, tp_price).
        """
        sl_distance = atr * cfg.ATR_SL_MULTIPLIER
        tp_distance = atr * cfg.ATR_TP_MULTIPLIER

        if is_long:
            sl_price = entry_price - sl_distance
            tp_price = entry_price + tp_distance
        else:
            sl_price = entry_price + sl_distance
            tp_price = entry_price - tp_distance

        return sl_price, tp_price

    def check_reward_risk_ratio(
        self,
        entry_price: float,
        atr: float,
        is_long: bool = True,
        regime: str = "RANGING",
    ) -> RiskDecision:
        """Check if the R:R ratio meets the regime-aware minimum threshold (GP-S2).

        Args:
            entry_price: The entry price for the trade.
            atr: Current ATR value.
            is_long: True for long position, False for short.
            regime: Market regime string for dynamic min R:R selection.

        Returns:
            RiskDecision with allowed=False if R:R below threshold.
        """
        sl_distance = atr * cfg.ATR_SL_MULTIPLIER
        tp_distance = atr * cfg.ATR_TP_MULTIPLIER

        reward_risk_ratio = tp_distance / sl_distance

        # GP-S2: regime-aware minimum R:R
        min_rr = self._regime_min_rr.get(regime, cfg.MIN_REWARD_RISK_RATIO)

        if reward_risk_ratio < min_rr:
            return RiskDecision(
                allowed=False,
                reason=f"R:R {reward_risk_ratio:.2f} below {min_rr} ({regime})",
            )

        return RiskDecision(allowed=True)

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
        # GP-S2: Reset intra-day counters (equity floor persists until restart)
        self._consecutive_losses = 0
        self._three_strike_cooldown_until = 0.0
        self._sync_daily_balance()
        return summary
