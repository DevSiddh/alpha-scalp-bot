"""Alpha-Scalp Bot – Order Execution Module.

Wraps CCXT calls to Bybit Futures for:
- Setting leverage
- Opening positions with market orders + SL/TP
- Closing positions
- Querying open positions
- Cancelling pending orders
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

import config as cfg
from risk_engine import RiskEngine

if TYPE_CHECKING:
    import ccxt


class OrderExecutor:
    """Handles all order lifecycle operations on Bybit via CCXT."""

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
    # Leverage
    # -----------------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for *symbol* on Bybit.

        Returns *True* on success, *False* on failure.
        """
        try:
            self.exchange.set_leverage(leverage, symbol)
            logger.info("Leverage set to {}x for {}", leverage, symbol)
            return True
        except Exception as exc:
            # Some exchanges silently accept if leverage is already set
            err_msg = str(exc).lower()
            if "not modified" in err_msg or "same leverage" in err_msg:
                logger.debug("Leverage already {}x for {} – no change needed", leverage, symbol)
                return True
            logger.error("Failed to set leverage for {}: {}", symbol, exc)
            return False

    # -----------------------------------------------------------------
    # Open Position
    # -----------------------------------------------------------------
    def open_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_loss: float,
        take_profit: float,
    ) -> dict[str, Any] | None:
        """Place a market order with attached stop-loss and take-profit.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. ``"BTC/USDT:USDT"``.
        side : str
            ``"buy"`` or ``"sell"``.
        amount : float
            Position size in base currency units.
        stop_loss : float
            Absolute stop-loss price.
        take_profit : float
            Absolute take-profit price.

        Returns
        -------
        dict | None
            CCXT order response on success, ``None`` on failure.
        """
        try:
            # Ensure leverage is set before placing the order
            self.set_leverage(symbol, self.risk.leverage)

            # Round amount to exchange precision
            market = self.exchange.market(symbol)
            amount = self.exchange.amount_to_precision(symbol, amount)
            amount_float = float(amount)

            if amount_float <= 0:
                logger.warning("Calculated amount is 0 – skipping order")
                return None

            # Build SL / TP params for Bybit
            params: dict[str, Any] = {
                "stopLoss": {
                    "type": "market",
                    "triggerPrice": stop_loss,
                },
                "takeProfit": {
                    "type": "market",
                    "triggerPrice": take_profit,
                },
            }

            logger.info(
                "OPENING {} {} | size={} | SL={:.2f} | TP={:.2f}",
                side.upper(),
                symbol,
                amount,
                stop_loss,
                take_profit,
            )

            order = self.exchange.create_order(
                symbol=symbol,
                type=self.order_type,
                side=side.lower(),
                amount=amount_float,
                params=params,
            )

            order_id = order.get("id", "unknown")
            avg_price = order.get("average") or order.get("price", 0)
            filled = order.get("filled", 0)

            logger.info(
                "ORDER FILLED | id={} | side={} | filled={} @ {:.2f} | "
                "SL={:.2f} | TP={:.2f}",
                order_id,
                side.upper(),
                filled,
                float(avg_price or 0),
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

        Detects side and size from open positions, then submits a
        counter-order to flatten.

        Returns
        -------
        dict | None
            CCXT order response, or ``None`` if nothing to close.
        """
        try:
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
        ``entryPrice``, ``unrealizedPnl``.
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
