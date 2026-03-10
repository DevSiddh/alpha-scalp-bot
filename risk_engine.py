"""Alpha-Scalp Bot – Risk Management Engine.

Handles:
- 3 % daily drawdown kill-switch
- Position sizing (1 % equity risk per trade)
- Stop-loss / take-profit calculation
- Max open-position enforcement
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

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
    # Balance helpers
    # -----------------------------------------------------------------
    def _fetch_usdt_balance(self) -> float:
        """Return total USDT equity from Binance Futures."""
        try:
            bal = self.exchange.fetch_balance({"type": "future"})
            total = float(bal.get("total", {}).get("USDT", 0.0))
            logger.debug("Fetched USDT balance: {:.4f}", total)
            return total
        except Exception as exc:
            logger.error("Failed to fetch balance: {}", exc)
            raise

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

        Compares current equity against *daily_start_balance*.
        Once triggered the flag stays active until :meth:`reset_daily`.
        """
        if self.kill_switch_active:
            return True

        try:
            current_balance = self._fetch_usdt_balance()
        except Exception:
            # If we can't read balance, err on the side of caution
            logger.warning("Balance read failed \u2013 activating kill switch")
            self.kill_switch_active = True
            return True

        if self.daily_start_balance <= 0:
            logger.warning("Daily start balance is zero \u2013 activating kill switch")
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
    # Position Sizing
    # -----------------------------------------------------------------
    def calculate_position_size(
        self, entry_price: float, stop_price: float
    ) -> float:
        """Calculate position size in *base currency* units.

        Uses ``RISK_PER_TRADE`` percentage of equity and the distance
        between *entry_price* and *stop_price* to derive the max
        position that keeps the dollar risk within budget.

        Returns
        -------
        float
            Position size in base units (e.g. BTC).
        """
        equity = self._fetch_usdt_balance()
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
    def get_stop_loss(self, entry_price: float, side: str) -> float:
        """Return absolute stop-loss price (0.5 % from entry)."""
        if side.upper() == "BUY":
            sl = entry_price * (1 - self.stop_loss_pct)
        else:
            sl = entry_price * (1 + self.stop_loss_pct)
        logger.debug("SL for {} @ {:.2f} -> {:.2f}", side, entry_price, sl)
        return round(sl, 2)

    def get_take_profit(self, entry_price: float, side: str) -> float:
        """Return absolute take-profit price (1.0 % from entry \u2013 2:1 R/R)."""
        if side.upper() == "BUY":
            tp = entry_price * (1 + self.take_profit_pct)
        else:
            tp = entry_price * (1 - self.take_profit_pct)
        logger.debug("TP for {} @ {:.2f} -> {:.2f}", side, entry_price, tp)
        return round(tp, 2)

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
