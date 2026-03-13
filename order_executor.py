"""Alpha-Scalp Bot – Order Execution Module (Binance Futures).

Wraps CCXT calls to Binance Futures for:
- Setting leverage and margin type
- Opening positions with market orders + separate SL/TP bracket orders
- Slippage enforcement after market fills
- Atomic bracket rollback on partial SL/TP failure
- Error classification (auth vs transient)
- Closing positions
- Querying open positions
- Cancelling pending orders
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Any

import ccxt
from loguru import logger

import config as cfg

if TYPE_CHECKING:
    from risk_engine import RiskEngine


# ---------------------------------------------------------------------------
# Custom exception for fatal auth errors
# ---------------------------------------------------------------------------
class FatalExchangeError(Exception):
    """Raised on unrecoverable exchange errors (auth, IP ban, etc.).

    The main loop should catch this and shut down instead of retrying.
    """
    pass


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


    # -------------------------------------------------------------------------
    # P0-1: Position Reconcile on Restart
    # -------------------------------------------------------------------------
    async def reconcile_position(self, trade_tracker) -> bool:
        """On startup: check for open positions/orders and restore into TradeTrackerV2.

        Returns True if an open position was found and restored.
        """
        try:
            positions = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.exchange.fetch_positions([self.symbol])
            )
            open_orders = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.exchange.fetch_open_orders(self.symbol)
            )

            active = [p for p in positions if abs(float(p.get("contracts", 0) or 0)) > 0]
            if not active:
                logger.info("reconcile_position | no open position found for {}", self.symbol)
                return False

            pos = active[0]
            side = "BUY" if float(pos.get("contracts", 0)) > 0 else "SELL"
            entry_price = float(pos.get("entryPrice") or pos.get("info", {}).get("entryPrice", 0))
            contracts = abs(float(pos.get("contracts", 0)))

            logger.warning(
                "reconcile_position | OPEN POSITION FOUND | side={} entry={:.4f} contracts={:.6f} — restoring into TradeTracker",
                side, entry_price, contracts,
            )

            # Find SL/TP from open bracket orders if available
            sl_price, tp_price = 0.0, 0.0
            for o in open_orders:
                otype = (o.get("type") or "").lower()
                if "stop" in otype:
                    sl_price = float(o.get("stopPrice") or o.get("price") or 0)
                elif otype in ("take_profit", "take_profit_market", "limit"):
                    tp_price = float(o.get("stopPrice") or o.get("price") or 0)

            trade_tracker.restore_open_position(
                symbol=self.symbol,
                side=side,
                entry_price=entry_price,
                contracts=contracts,
                sl_price=sl_price,
                tp_price=tp_price,
            )
            return True

        except Exception as exc:
            logger.error("reconcile_position | failed: {}", exc)
            return False

    # -----------------------------------------------------------------
    # Error Classification (Fix 3)
    # -----------------------------------------------------------------
    @staticmethod
    def _classify_error(exc: Exception) -> str:
        """Classify a CCXT exception as 'fatal' or 'transient'.

        Fatal errors (should trigger shutdown):
          - AuthenticationError (401, invalid API key)
          - AccountNotEnabled
          - PermissionDenied (403)
          - ExchangeNotAvailable (maintenance/IP ban)

        Transient errors (safe to retry):
          - NetworkError, RequestTimeout
          - RateLimitExceeded (will auto-throttle)
          - ExchangeError (generic, usually recoverable)
        """
        if isinstance(exc, (ccxt.AuthenticationError, ccxt.AccountNotEnabled)):
            return "fatal"
        if isinstance(exc, ccxt.PermissionDenied):
            return "fatal"
        if isinstance(exc, ccxt.ExchangeNotAvailable):
            # Could be maintenance or IP ban – treat as fatal
            return "fatal"
        if isinstance(exc, (ccxt.NetworkError, ccxt.RequestTimeout)):
            return "transient"
        if isinstance(exc, ccxt.RateLimitExceeded):
            return "transient"
        if isinstance(exc, ccxt.ExchangeError):
            return "transient"
        # Unknown – default to transient to avoid unnecessary shutdowns
        return "transient"

    def _handle_exchange_error(self, exc: Exception, context: str) -> None:
        """Log and optionally raise FatalExchangeError based on classification."""
        severity = self._classify_error(exc)
        if severity == "fatal":
            logger.critical(
                "FATAL EXCHANGE ERROR in {} | {} | {}", context, type(exc).__name__, exc
            )
            raise FatalExchangeError(
                f"Fatal exchange error in {context}: {exc}"
            ) from exc
        else:
            logger.warning(
                "Transient error in {} | {} | {} – will retry",
                context,
                type(exc).__name__,
                exc,
            )

    # -----------------------------------------------------------------
    # Margin Type
    # -----------------------------------------------------------------
    def set_margin_type(self, symbol: str | None = None) -> bool:
        """Set ISOLATED margin mode for *symbol* on Binance Futures."""
        symbol = symbol or self.symbol
        try:
            self.exchange.set_margin_mode("isolated", symbol)
            logger.info("Margin mode set to ISOLATED for {}", symbol)
            return True
        except Exception as exc:
            err_msg = str(exc).lower()
            if "no need to change" in err_msg or "already" in err_msg:
                logger.debug("Margin mode already ISOLATED for {} – no change needed", symbol)
                return True
            self._handle_exchange_error(exc, "set_margin_type")
            return False

    # -----------------------------------------------------------------
    # Leverage
    # -----------------------------------------------------------------
    def set_leverage(self, symbol: str | None = None, leverage: int | None = None) -> bool:
        """Set leverage for *symbol* on Binance Futures."""
        symbol = symbol or self.symbol
        leverage = leverage or self.risk.get_effective_leverage()
        try:
            self.exchange.set_leverage(leverage, symbol)
            logger.info("Leverage set to {}x for {}", leverage, symbol)
            return True
        except Exception as exc:
            err_msg = str(exc).lower()
            if "no need to change" in err_msg or "same leverage" in err_msg:
                logger.debug("Leverage already {}x for {} – no change needed", leverage, symbol)
                return True
            self._handle_exchange_error(exc, "set_leverage")
            return False

    # -----------------------------------------------------------------
    # Slippage Check (Fix 1)
    # -----------------------------------------------------------------
    def _check_slippage(
        self, expected_price: float, fill_price: float, side: str
    ) -> bool:
        """Return True if slippage is within tolerance.

        For BUY:  fill_price should be <= expected * (1 + tolerance)
        For SELL: fill_price should be >= expected * (1 - tolerance)
        """
        if expected_price <= 0:
            logger.warning("Expected price is 0 – skipping slippage check")
            return True

        slippage = (fill_price - expected_price) / expected_price

        if side.lower() == "buy":
            # Buying higher than expected is bad slippage
            within_tolerance = slippage <= self.slippage_tolerance
        else:
            # Selling lower than expected is bad slippage
            within_tolerance = (-slippage) <= self.slippage_tolerance

        logger.info(
            "SLIPPAGE CHECK | side={} | expected={:.2f} | filled={:.2f} | "
            "slippage={:+.4%} | tolerance={:.4%} | {}",
            side.upper(),
            expected_price,
            fill_price,
            slippage,
            self.slippage_tolerance,
            "PASS" if within_tolerance else "FAIL",
        )
        return within_tolerance

    def _emergency_flatten(self, symbol: str, side: str, amount: float) -> None:
        """Immediately close a position when slippage exceeds tolerance."""
        opposite = "sell" if side.lower() == "buy" else "buy"
        try:
            logger.critical(
                "SLIPPAGE EXCEEDED – emergency flatten {} {} {}",
                opposite.upper(), symbol, amount,
            )
            self.exchange.create_order(
                symbol=symbol,
                type="market",
                side=opposite,
                amount=amount,
                params={"reduceOnly": True},
            )
            logger.info("Emergency flatten complete for {}", symbol)
        except Exception as exc:
            logger.critical(
                "EMERGENCY FLATTEN FAILED for {} – MANUAL INTERVENTION REQUIRED: {}",
                symbol, exc,
            )

    # -----------------------------------------------------------------
    # Spread Guard (pre-execution safety check)
    # -----------------------------------------------------------------
    def check_spread(self, symbol: str) -> tuple[bool, float, dict]:
        """Check live bid-ask spread before placing a trade.

        Fetches the top N levels of the order book and calculates the
        spread as a percentage of the mid price.  If spread exceeds
        MAX_SPREAD_PCT, the trade should be aborted to avoid slippage.

        Returns
        -------
        tuple[bool, float, dict]
            (is_safe, spread_pct, details)
            is_safe: True if spread is within tolerance
            spread_pct: actual spread as fraction (e.g. 0.0003 = 0.03%)
            details: {best_bid, best_ask, mid_price, spread_pct, book_depth}
        """
        if not cfg.SPREAD_GUARD_ENABLED:
            return True, 0.0, {"skipped": True}

        try:
            book = self.exchange.fetch_order_book(
                symbol, limit=cfg.SPREAD_GUARD_BOOK_DEPTH
            )

            bids = book.get("bids", [])
            asks = book.get("asks", [])

            if not bids or not asks:
                logger.warning(
                    "SPREAD GUARD | Empty order book for {} – blocking trade",
                    symbol,
                )
                return False, 1.0, {"error": "empty_book"}

            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            mid_price = (best_bid + best_ask) / 2.0

            if mid_price <= 0:
                logger.warning("SPREAD GUARD | Invalid mid price – blocking trade")
                return False, 1.0, {"error": "invalid_mid"}

            spread_pct = (best_ask - best_bid) / mid_price
            is_safe = spread_pct <= cfg.MAX_SPREAD_PCT

            details = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid_price": mid_price,
                "spread_pct": spread_pct,
                "max_spread_pct": cfg.MAX_SPREAD_PCT,
                "bid_depth": sum(float(b[1]) for b in bids[:cfg.SPREAD_GUARD_BOOK_DEPTH]),
                "ask_depth": sum(float(a[1]) for a in asks[:cfg.SPREAD_GUARD_BOOK_DEPTH]),
            }

            if is_safe:
                logger.info(
                    "SPREAD GUARD PASS | {} | spread={:.4%} (max={:.4%}) | "
                    "bid={:.2f} ask={:.2f} mid={:.2f}",
                    symbol, spread_pct, cfg.MAX_SPREAD_PCT,
                    best_bid, best_ask, mid_price,
                )
            else:
                logger.warning(
                    "SPREAD GUARD BLOCKED | {} | spread={:.4%} > max={:.4%} | "
                    "bid={:.2f} ask={:.2f} | Slippage would kill PnL",
                    symbol, spread_pct, cfg.MAX_SPREAD_PCT,
                    best_bid, best_ask,
                )

            return is_safe, spread_pct, details

        except Exception as exc:
            logger.error(
                "SPREAD GUARD ERROR | {} | {} – allowing trade (fail-open)",
                symbol, exc,
            )
            # Fail-open: if we can't check spread, don't block the trade
            # (slippage check after fill is still the backstop)
            return True, 0.0, {"error": str(exc)}

    # -----------------------------------------------------------------
    # Atomic Bracket Rollback (Fix 2)
    # -----------------------------------------------------------------
    def _place_bracket_orders(
        self,
        symbol: str,
        opposite_side: str,
        amount_float: float,
        stop_loss: float,
        take_profit: float,
    ) -> tuple[dict | None, dict | None]:
        """Place SL and TP orders. If either fails, cancel the other and
        flatten the position to avoid naked exposure.

        Returns (sl_order, tp_order) – both None on rollback.
        """
        sl_order = None
        tp_order = None

        # --- Place SL ---
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
            logger.error("SL ORDER FAILED: {} – rolling back position", sl_exc)
            self._handle_exchange_error(sl_exc, "place_sl_order")
            # SL failed – no bracket protection, must flatten
            return None, None

        # --- Place TP ---
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
            logger.error("TP ORDER FAILED: {} – cancelling SL and rolling back", tp_exc)
            # Cancel the SL we just placed
            try:
                if sl_order and sl_order.get("id"):
                    self.exchange.cancel_order(sl_order["id"], symbol)
                    logger.info("Cancelled orphaned SL order {}", sl_order["id"])
            except Exception as cancel_exc:
                logger.error("Failed to cancel orphaned SL: {}", cancel_exc)
            self._handle_exchange_error(tp_exc, "place_tp_order")
            return None, None

        return sl_order, tp_order

    # -----------------------------------------------------------------
    # Open Position (with all 3 fixes integrated)
    # -----------------------------------------------------------------
    def open_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_loss: float,
        take_profit: float,
        expected_entry: float = 0.0,
    ) -> dict[str, Any] | None:
        """Place a market order with slippage check + atomic SL/TP bracket.

        Flow:
        1. Market order entry
        2. Slippage check – if FAIL, emergency flatten & abort
        3. Place SL order – if FAIL, flatten & abort
        4. Place TP order – if FAIL, cancel SL, flatten & abort
        5. Recalculate SL/TP from actual fill price (not expected)

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
        expected_entry : float
            Expected entry price for slippage comparison.
            If 0, slippage check is skipped.

        Returns
        -------
        dict | None
            CCXT order response on success, ``None`` on failure.
        """
        try:
            # Round amount to exchange precision
            amount = self.exchange.amount_to_precision(symbol, amount)
            amount_float = float(amount)

            if amount_float <= 0:
                logger.warning("Calculated amount is 0 – skipping order")
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

            # ---- Step 0: Spread guard (pre-execution safety) ----
            spread_safe, spread_pct, spread_details = self.check_spread(symbol)
            if not spread_safe:
                logger.warning(
                    "TRADE ABORTED by spread guard | {} | spread={:.4%} | {}",
                    symbol, spread_pct, spread_details,
                )
                return None

            # ---- Step 1: Market entry ----
            order = self.exchange.create_market_order(
                symbol=symbol,
                side=side.lower(),
                amount=amount_float,
            )

            order_id = order.get("id", "unknown")
            avg_price = order.get("average") or order.get("price", 0)
            filled = float(order.get("filled", 0))
            fill_price = float(avg_price) if avg_price else 0.0

            logger.info(
                "ENTRY FILLED | id={} | side={} | filled={} @ {:.2f}",
                order_id,
                side.upper(),
                filled,
                fill_price,
            )

            # ---- Step 2: Slippage check (Fix 1) ----
            if expected_entry > 0 and fill_price > 0:
                if not self._check_slippage(expected_entry, fill_price, side):
                    self._emergency_flatten(symbol, side, filled or amount_float)
                    return None

            # ---- Step 3: Recalculate SL/TP from actual fill price ----
            if fill_price > 0:
                actual_sl = self.risk.get_stop_loss(fill_price, side)
                actual_tp = self.risk.get_take_profit(fill_price, side)
                if actual_sl != stop_loss or actual_tp != take_profit:
                    logger.info(
                        "SL/TP adjusted for fill price | SL: {:.2f}->{:.2f} | TP: {:.2f}->{:.2f}",
                        stop_loss, actual_sl, take_profit, actual_tp,
                    )
                    stop_loss = actual_sl
                    take_profit = actual_tp

            # ---- Step 4: Atomic bracket (Fix 2) ----
            sl_order, tp_order = self._place_bracket_orders(
                symbol=symbol,
                opposite_side=opposite_side,
                amount_float=filled or amount_float,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )

            if sl_order is None or tp_order is None:
                # Bracket failed – flatten the position
                logger.critical(
                    "BRACKET INCOMPLETE – flattening position for safety"
                )
                self._emergency_flatten(symbol, side, filled or amount_float)
                return None

            logger.info(
                "BRACKET COMPLETE | entry={:.2f} | SL={:.2f} | TP={:.2f}",
                fill_price,
                stop_loss,
                take_profit,
            )

            # Attach fill info for caller
            order["_fill_price"] = fill_price
            order["_actual_sl"] = stop_loss
            order["_actual_tp"] = take_profit

            return order

        except FatalExchangeError:
            # Re-raise – main loop must catch this and shut down
            raise
        except Exception as exc:
            self._handle_exchange_error(exc, f"open_position {side} {symbol}")
            return None

    # -----------------------------------------------------------------
    # Close Position (enhanced with trade-tracking info)
    # -----------------------------------------------------------------
    def close_position(self, symbol: str) -> dict[str, Any] | None:
        """Close any open position for *symbol*.

        1. Fetch position info BEFORE closing (entry_price, side, size).
        2. Cancel all open orders (SL/TP) for the symbol.
        3. Place a counter market order to flatten.
        4. Return enriched dict with entry/exit prices for trade tracking.
        """
        try:
            # Capture position info BEFORE closing
            positions = self.exchange.fetch_positions([symbol])
            target_pos = None
            for pos in positions:
                contracts = float(pos.get("contracts", 0))
                if contracts > 0:
                    target_pos = pos
                    break

            if target_pos is None:
                logger.debug("No open position to close for {}", symbol)
                return None

            entry_price = float(target_pos.get("entryPrice", 0))
            pos_side = target_pos.get("side", "").lower()
            contracts = float(target_pos.get("contracts", 0))
            close_side = "sell" if pos_side == "long" else "buy"

            # Cancel bracket orders first
            self.cancel_all_orders(symbol)

            logger.info(
                "CLOSING {} position on {} | size={} | entry={:.2f}",
                pos_side.upper(),
                symbol,
                contracts,
                entry_price,
            )

            order = self.exchange.create_order(
                symbol=symbol,
                type="market",
                side=close_side,
                amount=contracts,
                params={"reduceOnly": True},
            )

            exit_price = float(order.get("average") or order.get("price", 0))

            logger.info(
                "POSITION CLOSED | id={} | side={} | filled={} | exit={:.2f}",
                order.get("id", "unknown"),
                close_side,
                order.get("filled", 0),
                exit_price,
            )

            # Enrich order with tracking info
            order["_entry_price"] = entry_price
            order["_exit_price"] = exit_price
            order["_side"] = pos_side  # "long" or "short"
            order["_size"] = contracts
            order["_symbol"] = symbol
            return order

        except Exception as exc:
            self._handle_exchange_error(exc, f"close_position {symbol}")
            return None

    # -----------------------------------------------------------------
    # Detect SL/TP fills (position monitor)
    # -----------------------------------------------------------------
    def get_position_info(self, symbol: str) -> dict[str, Any] | None:
        """Fetch current position info for a symbol. Returns None if no position."""
        try:
            positions = self.exchange.fetch_positions([symbol])
            for pos in positions:
                contracts = float(pos.get("contracts", 0))
                if contracts > 0:
                    return {
                        "symbol": symbol,
                        "side": pos.get("side", "unknown").lower(),
                        "contracts": contracts,
                        "entry_price": float(pos.get("entryPrice", 0)),
                        "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                    }
            return None
        except Exception as exc:
            logger.error("Failed to fetch position info for {}: {}", symbol, exc)
            return None

    # -----------------------------------------------------------------
    # Query Open Positions
    # -----------------------------------------------------------------
    def get_open_positions(self, symbol: str) -> list[dict[str, Any]]:
        """Return a list of open positions for *symbol*."""
        try:
            positions = self.exchange.fetch_positions([symbol])
            open_positions = [
                {
                    "symbol": p.get("symbol", symbol),
                    "side": p.get("side", "unknown"),
                    "contracts": float(p.get("contracts", 0)),
                    "entry_price": float(p.get("entryPrice", 0)),
                    "unrealized_pnl": float(p.get("unrealizedPnl", 0)),
                    "leverage": p.get("leverage", self.risk.get_effective_leverage()),
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
            self._handle_exchange_error(exc, f"get_open_positions {symbol}")
            return []

    # -----------------------------------------------------------------
    # Cancel All Orders
    # -----------------------------------------------------------------
    def cancel_all_orders(self, symbol: str) -> int:
        """Cancel every pending (open) order for *symbol*."""
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
            self._handle_exchange_error(exc, f"cancel_all_orders {symbol}")
            return 0
