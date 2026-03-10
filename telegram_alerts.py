"""Alpha-Scalp Bot – Telegram Alert Module.

Async Telegram Bot API integration via httpx for:
- Trade entry/exit alerts
- Kill-switch warnings
- Daily P&L summaries
- Error notifications
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from loguru import logger

import config as cfg

# Telegram Bot API base
_TG_API = "https://api.telegram.org"


class TelegramAlerts:
    """Sends formatted messages to a Telegram chat via Bot API."""

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self.bot_token: str = bot_token or cfg.TELEGRAM_BOT_TOKEN
        self.chat_id: str = chat_id or cfg.TELEGRAM_CHAT_ID
        self.enabled: bool = bool(self.bot_token and self.chat_id)

        if not self.enabled:
            logger.warning(
                "Telegram alerts DISABLED – bot_token or chat_id missing"
            )
        else:
            logger.info("TelegramAlerts initialised | chat_id={}", self.chat_id)

    # -----------------------------------------------------------------
    # Core sender
    # -----------------------------------------------------------------
    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send *text* to the configured Telegram chat.

        Returns *True* on success, *False* on failure.
        """
        if not self.enabled:
            logger.debug("Telegram disabled – message suppressed")
            return False

        url = f"{_TG_API}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                logger.debug("Telegram message sent ({} chars)", len(text))
                return True
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Telegram API error {}: {}",
                exc.response.status_code,
                exc.response.text[:300],
            )
            return False
        except Exception as exc:
            logger.error("Telegram send failed: {}", exc)
            return False

    # -----------------------------------------------------------------
    # Trade alerts
    # -----------------------------------------------------------------
    async def send_trade_alert(
        self,
        side: str,
        symbol: str,
        entry: float,
        sl: float,
        tp: float,
        size: float,
    ) -> bool:
        """Send a formatted trade-entry notification."""
        arrow = "UP" if side.upper() == "BUY" else "DOWN"
        side_label = "LONG" if side.upper() == "BUY" else "SHORT"
        risk_reward = (
            abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        )

        text = (
            f"<b>[{arrow}] [SCALP] NEW {side_label} | {symbol}</b>\n"
            f"\n"
            f"Entry  : <code>{entry:,.2f}</code>\n"
            f"Size   : <code>{size:.6f}</code>\n"
            f"SL     : <code>{sl:,.2f}</code>\n"
            f"TP     : <code>{tp:,.2f}</code>\n"
            f"R/R    : <code>{risk_reward:.1f}:1</code>\n"
            f"\n"
            f"<i>#{symbol.replace('/', '')} #Binance #scalp</i>"
        )
        return await self.send_message(text)

    # -----------------------------------------------------------------
    # Swing trade alert
    # -----------------------------------------------------------------
    async def send_swing_trade_alert(
        self,
        side: str,
        symbol: str,
        entry: float,
        sl: float,
        tp: float,
        size: float,
        confidence: float = 0.0,
        reason: str = "",
    ) -> bool:
        """Send a formatted swing trade notification."""
        side_label = "LONG" if side.upper() == "BUY" else "SHORT"
        risk_reward = (
            abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        )

        text = (
            f"<b>[SWING {side_label} {symbol.split('/')[0]}]</b>\n"
            f"\n"
            f"Pair   : <code>{symbol}</code>\n"
            f"Entry  : <code>{entry:,.2f}</code>\n"
            f"Size   : <code>{size:.6f}</code>\n"
            f"SL     : <code>{sl:,.2f}</code>\n"
            f"TP     : <code>{tp:,.2f}</code>\n"
            f"R/R    : <code>{risk_reward:.1f}:1</code>\n"
            f"Conf   : <code>{confidence:.0%}</code>\n"
            f"\n"
            f"<i>{reason}</i>\n"
            f"\n"
            f"<i>#{symbol.replace('/', '')} #Binance #swing</i>"
        )
        return await self.send_message(text)

    # -----------------------------------------------------------------
    # Trade close alert (for tracked trades)
    # -----------------------------------------------------------------
    async def send_trade_close_alert(
        self,
        symbol: str,
        side: str,
        trade_type: str,
        entry_price: float,
        exit_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        reason: str,
    ) -> bool:
        """Send a formatted trade-close notification."""
        pnl_icon = "PROFIT" if pnl_usdt >= 0 else "LOSS"
        reason_label = {
            "tp": "TP Hit",
            "sl": "SL Hit",
            "trailing": "Trailing Stop",
            "manual": "Manual Close",
        }.get(reason.lower(), reason.upper())

        text = (
            f"<b>[{pnl_icon}] [CLOSED] {trade_type.upper()} {side.upper()} {symbol}</b>\n"
            f"\n"
            f"Entry  : <code>{entry_price:,.2f}</code>\n"
            f"Exit   : <code>{exit_price:,.2f}</code>\n"
            f"P&L    : <code>{pnl_usdt:+,.2f} USDT ({pnl_pct:+.2f}%)</code>\n"
            f"Reason : <code>{reason_label}</code>\n"
            f"\n"
            f"<i>#{symbol.replace('/', '')} #{trade_type.lower()}</i>"
        )
        return await self.send_message(text)

    # -----------------------------------------------------------------
    # Stats message (for /stats command)
    # -----------------------------------------------------------------
    async def send_stats_message(
        self,
        session_stats: dict,
        cumulative_stats: dict,
    ) -> bool:
        """Send a formatted stats overview (session + all-time)."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        s = session_stats
        c = cumulative_stats

        # Format profit factor
        pf = c.get("profit_factor", 0)
        pf_str = f"{pf:.2f}" if pf != float("inf") else "INF"

        streak = c.get("current_streak", "0")

        text = (
            f"<b>[STATS] Alpha-Scalp Bot</b>\n"
            f"\n"
            f"<b>-- Session --</b>\n"
            f"Trades : <code>{s.get('total_trades', 0)}</code>\n"
            f"Win %  : <code>{s.get('win_rate', 0):.1%}</code>\n"
            f"P&L    : <code>{s.get('total_pnl', 0):+,.2f} USDT</code>\n"
            f"Best   : <code>{s.get('best_trade', 0):+,.2f} USDT</code>\n"
            f"Worst  : <code>{s.get('worst_trade', 0):+,.2f} USDT</code>\n"
            f"\n"
            f"<b>-- All Time --</b>\n"
            f"Trades : <code>{c.get('total_trades', 0)}</code>\n"
            f"Win %  : <code>{c.get('win_rate', 0):.1%}</code>\n"
            f"P&L    : <code>{c.get('total_pnl', 0):+,.2f} USDT</code>\n"
            f"Profit Factor: <code>{pf_str}</code>\n"
            f"Avg Win  : <code>{c.get('avg_win', 0):+,.2f} USDT</code>\n"
            f"Avg Loss : <code>{c.get('avg_loss', 0):+,.2f} USDT</code>\n"
            f"Streak   : <code>{streak} (current)</code>\n"
            f"\n"
            f"<i>Updated: {now}</i>"
        )
        return await self.send_message(text)

    # -----------------------------------------------------------------
    # Kill-switch alert
    # -----------------------------------------------------------------
    async def send_kill_switch_alert(self) -> bool:
        """Send an urgent warning when the daily kill switch activates."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        text = (
            f"<b>[WARNING] KILL SWITCH ACTIVATED</b>\n"
            f"\n"
            f"Daily drawdown limit ({cfg.DAILY_DRAWDOWN_LIMIT:.1%}) reached.\n"
            f"All trading halted until next UTC midnight.\n"
            f"\n"
            f"Time: <code>{now}</code>"
        )
        return await self.send_message(text)

    # -----------------------------------------------------------------
    # Daily summary
    # -----------------------------------------------------------------
    async def send_daily_summary(
        self,
        pnl: float,
        trades: int,
        win_rate: float,
        start_balance: float | None = None,
        end_balance: float | None = None,
        cumulative_stats: dict | None = None,
    ) -> bool:
        """Send an end-of-day performance summary with optional cumulative stats."""
        pnl_icon = "PROFIT" if pnl >= 0 else "LOSS"
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        balance_line = ""
        if start_balance is not None and end_balance is not None:
            balance_line = (
                f"Balance: <code>{start_balance:,.2f}</code> -> "
                f"<code>{end_balance:,.2f}</code> USDT\n"
            )

        cumulative_section = ""
        if cumulative_stats:
            c = cumulative_stats
            pf = c.get("profit_factor", 0)
            pf_str = f"{pf:.2f}" if pf != float("inf") else "INF"
            cumulative_section = (
                f"\n"
                f"<b>-- All Time --</b>\n"
                f"Total Trades : <code>{c.get('total_trades', 0)}</code>\n"
                f"Win %  : <code>{c.get('win_rate', 0):.1%}</code>\n"
                f"Total P&L : <code>{c.get('total_pnl', 0):+,.2f} USDT</code>\n"
                f"Profit Factor: <code>{pf_str}</code>\n"
                f"Streak : <code>{c.get('current_streak', '0')}</code>\n"
            )

        text = (
            f"<b>[{pnl_icon}] DAILY SUMMARY | {date_str}</b>\n"
            f"\n"
            f"P&L    : <code>{pnl:+,.2f} USDT</code>\n"
            f"Trades : <code>{trades}</code>\n"
            f"Win %  : <code>{win_rate:.1%}</code>\n"
            f"{balance_line}"
            f"{cumulative_section}"
            f"\n"
            f"<i>#daily #report</i>"
        )
        return await self.send_message(text)

    # -----------------------------------------------------------------
    # Error alert
    # -----------------------------------------------------------------
    async def send_error_alert(self, error: str | Exception) -> bool:
        """Send an error notification to the operator."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        text = (
            f"<b>[ERROR] Bot Error</b>\n"
            f"\n"
            f"<code>{str(error)[:500]}</code>\n"
            f"\n"
            f"Time: <code>{now}</code>"
        )
        return await self.send_message(text)

    # -----------------------------------------------------------------
    # Startup / shutdown
    # -----------------------------------------------------------------
    async def send_startup_message(self) -> bool:
        """Announce that the bot has started."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mode = "DEMO (Paper)" if cfg.BINANCE_DEMO_TRADING else "LIVE"
        text = (
            f"<b>[START] Alpha-Scalp Bot Online</b>\n"
            f"\n"
            f"Exchange : <code>Binance Futures</code>\n"
            f"Mode     : <code>{mode}</code>\n"
            f"Symbol   : <code>{cfg.SYMBOL}</code>\n"
            f"TF       : <code>{cfg.TIMEFRAME}</code>\n"
            f"Leverage : <code>{cfg.LEVERAGE}x</code>\n"
            f"Strategy : EMA {cfg.EMA_FAST}/{cfg.EMA_SLOW} + RSI {cfg.RSI_PERIOD} + NW Envelope\n"
        )
        swing_line = ""
        if cfg.SWING_ENABLED:
            swing_line = (
                f"\n"
                f"<b>Swing Mode:</b>\n"
                f"Symbols  : <code>{', '.join(cfg.SWING_SYMBOLS)}</code>\n"
                f"TF       : <code>{cfg.SWING_TIMEFRAME}</code>\n"
                f"Leverage : <code>{cfg.SWING_LEVERAGE}x</code>\n"
                f"SL/TP    : <code>{cfg.SWING_STOP_LOSS_PCT:.1%} / {cfg.SWING_TAKE_PROFIT_PCT:.1%}</code>\n"
                f"Strategy : EMA {cfg.SWING_EMA_FAST}/{cfg.SWING_EMA_SLOW} + RSI + S/R\n"
            )
        text += (
            f"{swing_line}"
            f"\n"
            f"Started  : <code>{now}</code>"
        )
        return await self.send_message(text)

    # -----------------------------------------------------------------
    # Telegram getUpdates (for /stats command polling)
    # -----------------------------------------------------------------
    async def get_updates(self, offset: int = 0, timeout: int = 1) -> tuple[list[dict], int]:
        """Poll Telegram getUpdates for new messages.

        Returns (list_of_updates, new_offset).
        """
        if not self.enabled:
            return [], offset

        url = f"{_TG_API}/bot{self.bot_token}/getUpdates"
        params = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": '["message"]',
        }

        try:
            async with httpx.AsyncClient(timeout=timeout + 5) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            updates = data.get("result", [])
            new_offset = offset
            if updates:
                new_offset = updates[-1]["update_id"] + 1
            return updates, new_offset
        except Exception as exc:
            logger.debug("getUpdates failed: {}", exc)
            return [], offset

    async def send_shutdown_message(self, reason: str = "Manual stop") -> bool:
        """Announce that the bot is shutting down."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = (
            f"<b>[STOP] Alpha-Scalp Bot Offline</b>\n"
            f"\n"
            f"Reason : <code>{reason}</code>\n"
            f"Time   : <code>{now}</code>"
        )
        return await self.send_message(text)
