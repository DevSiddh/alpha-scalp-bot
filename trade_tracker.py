"""Alpha-Scalp Bot – Trade Tracker Module.

Maintains an in-memory list of closed trades and persists them to
``logs/trade_history.json``.  Provides daily, session, and cumulative
stats for P&L reporting and the /stats Telegram command.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

import config as cfg


class TradeTracker:
    """Records closed trades, persists to JSON, and computes statistics."""

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    def __init__(self, history_file: str | None = None) -> None:
        self.history_file = Path(history_file or cfg.TRADE_HISTORY_FILE)
        self.history_file.parent.mkdir(parents=True, exist_ok=True)

        # All-time trade history (loaded from disk)
        self._trades: list[dict[str, Any]] = []

        # Session trades (since this process started)
        self._session_trades: list[dict[str, Any]] = []

        # Daily boundary marker (ISO date string)
        self._daily_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Thread-safe lock for file writes (works in async context too)
        self._lock = asyncio.Lock()

        # Streak tracking
        self._current_streak: int = 0  # positive = wins, negative = losses
        self._longest_win_streak: int = 0
        self._longest_lose_streak: int = 0

        # Load existing history
        self.load_history()
        logger.info(
            "TradeTracker initialised | {} historical trades loaded | file={}",
            len(self._trades),
            self.history_file,
        )

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------
    def load_history(self) -> None:
        """Load trade history from JSON file on startup."""
        if not self.history_file.exists():
            logger.debug("No trade history file found – starting fresh")
            return
        try:
            with open(self.history_file, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._trades = data
                # Rebuild streak from history
                self._rebuild_streaks()
                logger.info("Loaded {} trades from {}", len(self._trades), self.history_file)
            else:
                logger.warning("Trade history file has unexpected format – ignoring")
        except json.JSONDecodeError as exc:
            logger.error("Corrupt trade history JSON: {} – starting fresh", exc)
        except Exception as exc:
            logger.error("Failed to load trade history: {}", exc)

    async def _save_history(self) -> None:
        """Persist the full trade list to JSON (atomic write)."""
        async with self._lock:
            try:
                tmp_file = self.history_file.with_suffix(".tmp")
                with open(tmp_file, "w") as f:
                    json.dump(self._trades, f, indent=2, default=str)
                tmp_file.replace(self.history_file)
                logger.debug(
                    "Trade history saved ({} trades) -> {}",
                    len(self._trades),
                    self.history_file,
                )
            except Exception as exc:
                logger.error("Failed to save trade history: {}", exc)

    def _rebuild_streaks(self) -> None:
        """Rebuild streak counters from full trade history."""
        self._current_streak = 0
        self._longest_win_streak = 0
        self._longest_lose_streak = 0
        win_run = 0
        lose_run = 0
        for t in self._trades:
            if t.get("is_win"):
                win_run += 1
                lose_run = 0
                self._longest_win_streak = max(self._longest_win_streak, win_run)
            else:
                lose_run += 1
                win_run = 0
                self._longest_lose_streak = max(self._longest_lose_streak, lose_run)
        # Current streak from the tail
        if self._trades:
            last_win = self._trades[-1].get("is_win")
            streak = 0
            for t in reversed(self._trades):
                if t.get("is_win") == last_win:
                    streak += 1
                else:
                    break
            self._current_streak = streak if last_win else -streak

    # -----------------------------------------------------------------
    # Record a closed trade
    # -----------------------------------------------------------------
    async def record_trade(
        self,
        symbol: str,
        side: str,
        trade_type: str,
        entry_price: float,
        exit_price: float,
        size: float,
        reason: str,
        entry_time: str | None = None,
    ) -> dict[str, Any]:
        """Calculate P&L, append to history, persist, and return the record."""
        now = datetime.now(timezone.utc)

        # P&L calculation
        if side.lower() == "long":
            pnl_usdt = (exit_price - entry_price) * size
        else:  # short
            pnl_usdt = (entry_price - exit_price) * size

        pnl_pct = (pnl_usdt / (entry_price * size)) * 100 if (entry_price * size) > 0 else 0.0
        is_win = pnl_usdt > 0

        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "symbol": symbol,
            "side": side.lower(),
            "trade_type": trade_type.lower(),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "size": size,
            "pnl_usdt": round(pnl_usdt, 4),
            "pnl_pct": round(pnl_pct, 4),
            "is_win": is_win,
            "entry_time": entry_time or now.isoformat(),
            "exit_time": now.isoformat(),
            "reason": reason.lower(),
        }

        self._trades.append(record)
        self._session_trades.append(record)

        # Update streaks
        if is_win:
            if self._current_streak > 0:
                self._current_streak += 1
            else:
                self._current_streak = 1
            self._longest_win_streak = max(self._longest_win_streak, self._current_streak)
        else:
            if self._current_streak < 0:
                self._current_streak -= 1
            else:
                self._current_streak = -1
            self._longest_lose_streak = max(self._longest_lose_streak, abs(self._current_streak))

        await self._save_history()

        logger.info(
            "Trade recorded | {} {} {} | entry={:.2f} exit={:.2f} | "
            "P&L={:+.2f} USDT ({:+.2f}%) | reason={} | total={}",
            trade_type.upper(),
            side.upper(),
            symbol,
            entry_price,
            exit_price,
            pnl_usdt,
            pnl_pct,
            reason,
            len(self._trades),
        )
        return record

    # -----------------------------------------------------------------
    # Daily stats
    # -----------------------------------------------------------------
    def get_daily_stats(self) -> dict[str, Any]:
        """Stats for trades closed today (UTC)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = [t for t in self._trades if t["exit_time"][:10] == today]
        return self._compute_stats(daily, label="daily")

    # -----------------------------------------------------------------
    # Session stats (since bot started)
    # -----------------------------------------------------------------
    def get_session_stats(self) -> dict[str, Any]:
        """Stats since this bot process started."""
        return self._compute_stats(self._session_trades, label="session")

    # -----------------------------------------------------------------
    # Cumulative (all-time) stats
    # -----------------------------------------------------------------
    def get_cumulative_stats(self) -> dict[str, Any]:
        """All-time stats across the full trade history."""
        stats = self._compute_stats(self._trades, label="cumulative")
        # Add streak info
        stats["longest_win_streak"] = self._longest_win_streak
        stats["longest_lose_streak"] = self._longest_lose_streak
        if self._current_streak > 0:
            stats["current_streak"] = f"{self._current_streak}W"
        elif self._current_streak < 0:
            stats["current_streak"] = f"{abs(self._current_streak)}L"
        else:
            stats["current_streak"] = "0"
        return stats

    # -----------------------------------------------------------------
    # Reset daily boundary
    # -----------------------------------------------------------------
    def reset_daily(self) -> None:
        """Called at UTC midnight – updates the daily date marker."""
        self._daily_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        logger.info("TradeTracker daily reset – new date: {}", self._daily_date)

    # -----------------------------------------------------------------
    # Internal stats computation
    # -----------------------------------------------------------------
    @staticmethod
    def _compute_stats(trades: list[dict[str, Any]], label: str = "") -> dict[str, Any]:
        """Compute stats from a list of trade records."""
        total = len(trades)
        wins = [t for t in trades if t.get("is_win")]
        losses = [t for t in trades if not t.get("is_win")]
        total_wins = len(wins)
        total_losses = len(losses)

        total_pnl = sum(t["pnl_usdt"] for t in trades)
        win_pnls = [t["pnl_usdt"] for t in wins]
        loss_pnls = [t["pnl_usdt"] for t in losses]

        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0

        gross_profit = sum(win_pnls) if win_pnls else 0.0
        gross_loss = abs(sum(loss_pnls)) if loss_pnls else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

        best_trade = max(trades, key=lambda t: t["pnl_usdt"])["pnl_usdt"] if trades else 0.0
        worst_trade = min(trades, key=lambda t: t["pnl_usdt"])["pnl_usdt"] if trades else 0.0

        return {
            "label": label,
            "total_trades": total,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "win_rate": (total_wins / total) if total > 0 else 0.0,
            "total_pnl": round(total_pnl, 2),
            "best_trade": round(best_trade, 2),
            "worst_trade": round(worst_trade, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
        }
