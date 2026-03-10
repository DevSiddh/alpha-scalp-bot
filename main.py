"""Alpha-Scalp Bot – Main Entry Point.

Orchestrates the full scalping loop:
1. Initialise exchange, strategy, risk engine, order executor, alerts
2. Continuously fetch candles, generate signals, execute trades
3. Enforce kill-switch and daily reset at UTC midnight
4. Graceful shutdown on SIGINT / SIGTERM
5. Fatal error classification – auto-shutdown on auth/permission errors

Usage:
    python main.py
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, timezone

import ccxt
import pandas as pd
from loguru import logger

import config as cfg
from order_executor import FatalExchangeError, OrderExecutor
from risk_engine import RiskEngine
from strategy import ScalpStrategy, Signal
from telegram_alerts import TelegramAlerts

# ---------------------------------------------------------------------------
# Exchange factory
# ---------------------------------------------------------------------------
def _create_exchange() -> ccxt.Exchange:
    """Instantiate and configure the Binance Futures CCXT client."""
    common_cfg = {
        "apiKey": cfg.BINANCE_API_KEY,
        "secret": cfg.BINANCE_SECRET,
        "enableRateLimit": True,
        "options": {
            "adjustForTimeDifference": True,
        },
    }

    if cfg.BINANCE_DEMO_TRADING:
        # Binance Demo Trading (replaced old testnet/sandbox)
        # Requires CCXT >= 4.5.6 and demo-specific API keys from:
        # https://www.binance.com/en/support/faq/detail/9be58f73e5e14338809e3b705b9687dd
        exchange = ccxt.binance({**common_cfg, "options": {**common_cfg["options"], "defaultType": "future"}})
        exchange.enable_demo_trading(True)
        logger.info("Exchange: Binance Futures DEMO TRADING (paper)")
    else:
        exchange = ccxt.binance({**common_cfg, "options": {**common_cfg["options"], "defaultType": "future"}})
        logger.warning("Exchange: Binance Futures LIVE – real funds at risk")

    # Verify connectivity
    exchange.load_markets()
    logger.info(
        "Markets loaded | {} pairs available", len(exchange.markets)
    )
    return exchange

# ---------------------------------------------------------------------------
# OHLCV fetcher (with error classification – Fix 3)
# ---------------------------------------------------------------------------
def fetch_ohlcv(exchange: ccxt.Exchange) -> pd.DataFrame | None:
    """Fetch recent OHLCV candles and return a DataFrame.

    Raises FatalExchangeError on auth/permission failures so the main
    loop can distinguish retryable errors from shutdown-worthy ones.
    """
    try:
        raw = exchange.fetch_ohlcv(
            cfg.SYMBOL, cfg.TIMEFRAME, limit=cfg.LOOKBACK_CANDLES
        )
        if not raw:
            logger.warning("Empty OHLCV response")
            return None

        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        logger.debug(
            "Fetched {} candles | latest close={:.2f}",
            len(df),
            df["close"].iloc[-1],
        )
        return df

    except (ccxt.AuthenticationError, ccxt.AccountNotEnabled, ccxt.PermissionDenied) as exc:
        logger.critical("FATAL: OHLCV fetch hit auth error – {}", exc)
        raise FatalExchangeError(f"Auth error in fetch_ohlcv: {exc}") from exc
    except ccxt.ExchangeNotAvailable as exc:
        logger.critical("FATAL: Exchange unavailable (maintenance/IP ban) – {}", exc)
        raise FatalExchangeError(f"Exchange unavailable: {exc}") from exc
    except Exception as exc:
        logger.error("OHLCV fetch failed (transient): {}", exc)
        return None

# ---------------------------------------------------------------------------
# Midnight detection
# ---------------------------------------------------------------------------
def _is_new_utc_day(last_date: str) -> tuple[bool, str]:
    """Check whether we've crossed into a new UTC day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return today != last_date, today

# ---------------------------------------------------------------------------
# Main async loop
# ---------------------------------------------------------------------------
async def run_bot() -> None:  # noqa: C901 – intentionally cohesive
    """Core trading loop."""

    # --- Initialisation ---------------------------------------------------
    logger.info("Initialising Alpha-Scalp Bot (Binance Futures)...")

    try:
        exchange = _create_exchange()
    except Exception as exc:
        logger.critical("Exchange init failed: {}", exc)
        sys.exit(1)

    risk = RiskEngine(exchange)
    strategy = ScalpStrategy()
    executor = OrderExecutor(exchange, risk)
    alerts = TelegramAlerts()

    # Set margin type and leverage once at startup
    executor.set_margin_type(cfg.SYMBOL)
    executor.set_leverage(cfg.SYMBOL, cfg.LEVERAGE)

    # Send startup alert
    await alerts.send_startup_message()
    logger.info("Bot started – entering main loop")

    # Track current UTC date for midnight reset
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- Shutdown flag (set by signal handlers) ---------------------------
    shutdown_event = asyncio.Event()

    def _handle_signal(sig: int, _frame) -> None:
        sig_name = signal.Signals(sig).name
        logger.warning("Received {} – initiating graceful shutdown", sig_name)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # --- Main loop --------------------------------------------------------
    while not shutdown_event.is_set():
        try:
            # 1. Midnight reset
            is_new_day, today = _is_new_utc_day(current_date)
            if is_new_day:
                logger.info("UTC midnight crossed – resetting daily stats")
                summary = risk.reset_daily()
                await alerts.send_daily_summary(
                    pnl=summary["pnl"],
                    trades=summary["trades"],
                    win_rate=summary["win_rate"],
                    start_balance=summary["start_balance"],
                    end_balance=summary["end_balance"],
                )
                current_date = today

            # 2. Kill switch (also refreshes balance cache for this loop)
            if risk.check_kill_switch():
                if not getattr(run_bot, "_ks_alerted", False):
                    await alerts.send_kill_switch_alert()
                    run_bot._ks_alerted = True  # type: ignore[attr-defined]
                logger.warning(
                    "Kill switch active – sleeping {} s", cfg.LOOP_INTERVAL * 12
                )
                await asyncio.sleep(cfg.LOOP_INTERVAL * 12)  # back off
                continue
            else:
                run_bot._ks_alerted = False  # type: ignore[attr-defined]

            # 3. Fetch candles
            df = fetch_ohlcv(exchange)
            if df is None or df.empty:
                logger.warning("No candle data – retrying in {} s", cfg.LOOP_INTERVAL)
                await asyncio.sleep(cfg.LOOP_INTERVAL)
                continue

            # 4. Generate signal
            trade_signal = strategy.calculate_signals(df)

            # 5. Act on signal
            if trade_signal.signal in (Signal.BUY, Signal.SELL):
                # Check if we can open a new position
                can_trade = risk.check_max_positions()
                if not can_trade:
                    logger.info("Max positions reached – skipping signal")
                else:
                    side = trade_signal.signal.value.lower()  # "buy" / "sell"
                    entry = trade_signal.entry_price

                    sl = risk.get_stop_loss(entry, side)
                    tp = risk.get_take_profit(entry, side)
                    size = risk.calculate_position_size(entry, sl)

                    if size > 0:
                        logger.info(
                            "Executing {} | entry={:.2f} | SL={:.2f} | TP={:.2f} | size={:.6f}",
                            side.upper(),
                            entry,
                            sl,
                            tp,
                            size,
                        )

                        order = executor.open_position(
                            symbol=cfg.SYMBOL,
                            side=side,
                            amount=size,
                            stop_loss=sl,
                            take_profit=tp,
                            expected_entry=entry,  # Fix 1: slippage check
                        )

                        # Invalidate balance cache after trade execution
                        risk.invalidate_balance_cache()

                        if order:
                            fill_price = float(order.get("_fill_price", entry))
                            actual_sl = float(order.get("_actual_sl", sl))
                            actual_tp = float(order.get("_actual_tp", tp))
                            await alerts.send_trade_alert(
                                side=side,
                                symbol=cfg.SYMBOL,
                                entry=fill_price,
                                sl=actual_sl,
                                tp=actual_tp,
                                size=size,
                            )
                        else:
                            logger.warning(
                                "Order returned None – slippage reject or bracket failure"
                            )
                    else:
                        logger.warning("Position size is 0 – skipping")

        # --- Fix 3: Fatal error = shutdown, transient = retry ---
        except FatalExchangeError as exc:
            logger.critical("FATAL ERROR – shutting down: {}", exc)
            await alerts.send_error_alert(f"FATAL: {exc}")
            shutdown_event.set()
            break

        except Exception as exc:
            logger.exception("Unhandled error in main loop: {}", exc)
            await alerts.send_error_alert(exc)

        # 6. Sleep
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=cfg.LOOP_INTERVAL
            )
        except asyncio.TimeoutError:
            pass  # normal – just loop again

    # --- Graceful shutdown ------------------------------------------------
    logger.info("Shutting down – closing open positions...")
    try:
        executor.close_position(cfg.SYMBOL)
        executor.cancel_all_orders(cfg.SYMBOL)
    except Exception as exc:
        logger.error("Cleanup error: {}", exc)

    await alerts.send_shutdown_message(reason="Graceful shutdown (signal)")
    logger.info("Alpha-Scalp Bot stopped.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Synchronous entry point."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt – exiting")
    except Exception as exc:
        logger.critical("Fatal error: {}", exc)
        sys.exit(1)

if __name__ == "__main__":
    main()
