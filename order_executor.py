"""Alpha-Scalp Bot – Order Execution Module (Binance Futures).

Wraps CCXT calls to Binance Futures for:
- Setting leverage and margin type
- Opening positions with market orders + separate SL/TP bracket orders
- Closing positions
- Querying open positions
- Cancelling pending orders
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

import config as cfg

if TYPE_CHECKING:
    import ccxt
    from risk_engine import RiskEngine


class OrderExecutor:
    """Handles all order lifecycle operations on Binance Futures via CCXT."""

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    def __init__(self, exchange: ccxt.Exchange, risk_engine: RiskEngine) -> None:
        self.exchange = exchange
        self.risk = risk_engine
        self.symbol: str = cfg.SYMBOL
        self.order_type: str = cfg.ORDER_TYPE
        self.slippage_tolerance: float = cfg.SLIPPAGE_TOLERANCE

        logger.info(
            "OrderExecutor initialised | symbol={} | order_type={} | slippage={:.2%}",
            self.symbol,
            self.order_type,
            self.slippage_tolerance,
        )

    # -----------------------------------------------------------------
    # Margin Type
    # -----------------------------------------------------------------
    def set_margin_type(self, symbol: str | None = None) -> bool:
        """Set ISOLATED margin mode for *symbol* on Binance Futures.

        Returns *True* on success (or already set), *False* on failure.
        """
        symbol = symbol or self.symbol
        try:
            self.exchange.set_margin_mode("isolated", symbol)
            logger.info("Margin mode set to ISOLATED for {}", symbol)
            return True
        except Exception as exc:
            err_msg = str(exc).lower()
            # Binance returns an error if margin type is already set
            if "no need to change" in err_msg or "already" in err_msg:
                logger.debug("Margin mode already ISOLATED for {} \u2013 no change needed", symbol)
                return True
            logger.error("Failed to set margin mode for {}: {}", symbol, exc)
            return False

    # -----------------------------------------------------------------
    # Leverage
    # -----------------------------------------------------------------
    def set_leverage(self, symbol: str | None = None, leverage: int | None = None) -> bool:
        """Set leverage for *symbol* on Binance Futures.

        Returns *True* on success, *False* on failure.
        """
        symbol = symbol or self.symbol
        leverage = leverage or self.risk.leverage
        try:
            self.exchange.set_leverage(leverage, symbol)
            logger.info("Leverage set to {}x for {}", leverage, symbol)
            return True
        except Exception as exc:
            err_msg = str(exc).lower()
            if "no need to change" in err_msg or "same leverage" in err_msg:
                logger.debug("Leverage already {}x for {} \u2013 no change needed", leverage, symbol)
                return True
            logger.error("Failed to set leverage for {}: {}", symbol, exc)
            return False

    # -----------------------------------------------------------------
    # Open Position (with separate SL + TP bracket orders)
    # -----------------------------------------------------------------
    def open_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_loss: float,
        take_profit: float,
    ) -> dict[str, Any] | None:
        """Place a market order with separate STOP_MARKET and TAKE_PROFIT_MARKET orders.

        Binance Futures does not support SL/TP as params on the market order
        itself, so we place them as independent conditional orders with
        ``closePosition=True``.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. ``"BTC/USDT"``.
        side : str
            ``"buy"`` or ``"sell"``.
        amount : float
            Position size in base currency units.
        stop_loss : float
            Absolute stop-loss trigger price.
        take_profit : float
            Absolute take-profit trigger price.

        Returns
        -------
        dict | None
            CCXT order response on success, ``None`` on failure.
        """
        try:
            # Round amount to exchange precision
            market = self.exchange.market(symbol)
            amount = self.exchange.amount_to_precision(symbol, amount)
            amount_float = float(amount)

            if amount_float <= 0:
                logger.warning("Calculated amount is 0 \u2013 skipping order")
                return None

            opposite_side = "sell" if side.lower() == "buy" else "buy"

            logger.info(
                "OPENING {} {} | size={} | SL={:.2f} | TP={:.2f}",
                side.upper(),
                symbol,
                amount,
                stop_loss,
                take_profit,
            )

            # 1) Place market entry order
            order = self.exchange.create_market_order(
                symbol=symbol,
                side=side.lower(),
                amount=amount_float,
            )

            order_id = order.get("id", "unknown")
            avg_price = order.get("average") or order.get("price", 0)
            filled = order.get("filled", 0)

            # Use fill price for bracket orders if available
            entry_price = float(avg_price) if avg_price else stop_loss

            logger.info(
                "ENTRY FILLED | id={} | side={} | filled={} @ {:.2f}",
                order_id,
                side.upper(),
                filled,
                entry_price,
            )

            # 2) Place stop-loss (STOP_MARKET) \u2013 closes entire position
            try:
                sl_order = self.exchange.create_order(
                    symbol=symbol,
                    type="STOP_MARKET",
                    side=opposite_side,
                    amount=amount_float,
                    price=None,
                    params={
                        "stopPrice": self.exchange.price_to_precision(symbol, stop_loss),
                        "closePosition": True,
                    },
                )
                logger.info(
                    "SL ORDER placed | id={} | trigger={:.2f}",
                    sl_order.get("id", "unknown"),
                    stop_loss,
                )
            except Exception as sl_exc:
                logger.error("Failed to place SL order: {}", sl_exc)

            # 3) Place take-profit (TAKE_PROFIT_MARKET) \u2013 closes entire position
            try:
                tp_order = self.exchange.create_order(
                    symbol=symbol,
                    type="TAKE_PROFIT_MARKET",
                    side=opposite_side,
                    amount=amount_float,
                    price=None,
                    params={
                        "stopPrice": self.exchange.price_to_precision(symbol, take_profit),
                        "closePosition": True,
                    },
                )
                logger.info(
                    "TP ORDER placed | id={} | trigger={:.2f}",
                    tp_order.get("id", "unknown"),
                    take_profit,
                )
            except Exception as tp_exc:
                logger.error("Failed to place TP order: {}", tp_exc)

            logger.info(
                "BRACKET COMPLETE | entry={:.2f} | SL={:.2f} | TP={:.2f}",
                entry_price,
                stop_loss,
                take_profit,
            )

            return order

        except Exception as exc:
            logger.error(
                "ORDER FAILED | {} {} {} | error: {}",
                side.upper(),
                symbol,
                amount,
                exc,
            )
            return None

    # -----------------------------------------------------------------
    # Close Position
    # -----------------------------------------------------------------
    def close_position(self, symbol: str) -> dict[str, Any] | None:
        """Close any open position for *symbol*.

        1. Cancel all open orders (SL/TP) for the symbol.
        2. Detect position side and size.
        3. Place a counter market order to flatten.

        Returns
        -------
        dict | None
            CCXT order response, or ``None`` if nothing to close.
        """
        try:
            # Cancel all conditional orders first (SL/TP)
            self.cancel_all_orders(symbol)

            positions = self.exchange.fetch_positions([symbol])
            for pos in positions:
                contracts = float(pos.get("contracts", 0))
                if contracts <= 0:
                    continue

                pos_side = pos.get("side", "").lower()  # "long" or "short"
                close_side = "sell" if pos_side == "long" else "buy"

                logger.info(
                    "CLOSING {} position on {} | size={}",
                    pos_side.upper(),
                    symbol,
                    contracts,
                )

                order = self.exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=close_side,
                    amount=contracts,
                    params={"reduceOnly": True},
                )

                logger.info(
                    "POSITION CLOSED | id={} | side={} | filled={}",
                    order.get("id", "unknown"),
                    close_side,
                    order.get("filled", 0),
                )
                return order

            logger.debug("No open position to close for {}", symbol)
            return None

        except Exception as exc:
            logger.error("Failed to close position for {}: {}", symbol, exc)
            return None

    # -----------------------------------------------------------------
    # Query Open Positions
    # -----------------------------------------------------------------
    def get_open_positions(self, symbol: str) -> list[dict[str, Any]]:
        """Return a list of open positions for *symbol*.

        Each dict contains at minimum: ``side``, ``contracts``,
        ``entry_price``, ``unrealized_pnl``.
        """
        try:
            positions = self.exchange.fetch_positions([symbol])
            open_positions = [
                {
                    "symbol": p.get("symbol", symbol),
                    "side": p.get("side", "unknown"),
                    "contracts": float(p.get("contracts", 0)),
                    "entry_price": float(p.get("entryPrice", 0)),
                    "unrealized_pnl": float(p.get("unrealizedPnl", 0)),
                    "leverage": p.get("leverage", self.risk.leverage),
                    "liquidation_price": p.get("liquidationPrice"),
                }
                for p in positions
                if float(p.get("contracts", 0)) > 0
            ]

            if open_positions:
                logger.debug(
                    "Open positions for {}: {}", symbol, open_positions
                )
            return open_positions

        except Exception as exc:
            logger.error("Failed to fetch positions for {}: {}", symbol, exc)
            return []

    # -----------------------------------------------------------------
    # Cancel All Orders
    # -----------------------------------------------------------------
    def cancel_all_orders(self, symbol: str) -> int:
        """Cancel every pending (open) order for *symbol*.

        Returns
        -------
        int
            Number of orders successfully cancelled.
        """
        try:
            open_orders = self.exchange.fetch_open_orders(symbol)
            if not open_orders:
                logger.debug("No pending orders to cancel for {}", symbol)
                return 0

            cancelled = 0
            for order in open_orders:
                try:
                    self.exchange.cancel_order(order["id"], symbol)
                    cancelled += 1
                    logger.info(
                        "Cancelled order {} ({})",
                        order["id"],
                        order.get("type", "unknown"),
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not cancel order {}: {}", order["id"], exc
                    )

            logger.info(
                "Cancelled {}/{} orders for {}",
                cancelled,
                len(open_orders),
                symbol,
            )
            return cancelled

        except Exception as exc:
            logger.error(
                "Failed to fetch/cancel orders for {}: {}", symbol, exc
            )
            return 0
