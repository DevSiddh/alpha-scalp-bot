"""Alpha-Scalp Bot – Trade Tracker V2 Module (Phase 1 Upgrade).

Extends the original TradeTracker with:
1. Signal Attribution — records which signals triggered each trade
2. EV Tracking — expected value calculation per signal and overall
3. Per-Signal Win Rates — tracks which signals actually make money
4. Score History — logs the scoring breakdown for every trade

This replaces trade_tracker.py.  All original functionality is preserved.
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


class TradeTrackerV2:
    """Records closed trades with full signal attribution and EV tracking."""

    # Binance VIP 0 taker fee (0.1% per side, 0.2% round trip)
    FEE_RATE: float = 0.001

    def __init__(self, history_file: str | None = None) -> None:
        self.history_file = Path(history_file or cfg.TRADE_HISTORY_FILE).with_suffix(".jsonl")
        self.history_file.parent.mkdir(parents=True, exist_ok=True)

        # All-time trade history
        self._trades: list[dict[str, Any]] = []

        # Session trades (since process started)
        self._session_trades: list[dict[str, Any]] = []

        # Daily boundary marker
        self._daily_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Thread-safe lock
        self._lock = asyncio.Lock()

        # Streak tracking
        self._current_streak: int = 0
        self._longest_win_streak: int = 0
        self._longest_lose_streak: int = 0

        # ===== NEW: Per-signal performance tracking =====
        self._signal_stats: dict[str, dict[str, Any]] = {}
        # Format: {"ema_cross": {"trades": 10, "wins": 7, "total_pnl": 45.2, ...}}

        # Load existing history
        self.load_history()
        logger.info(
            "TradeTrackerV2 initialised | {} historical trades | file={}",
            len(self._trades), self.history_file,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load_history(self) -> None:
        """Load trade history from JSONL file on startup (one JSON object per line)."""
        if not self.history_file.exists():
            logger.debug("No trade history file found – starting fresh")
            return
        try:
            loaded = 0
            with open(self.history_file, "r") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        self._trades.append(record)
                        loaded += 1
                    except json.JSONDecodeError as exc:
                        logger.warning("Skipping corrupt line {} in history: {}", line_num, exc)
            self._rebuild_streaks()
            self._rebuild_signal_stats()
            logger.info("Loaded {} trades from {}", loaded, self.history_file)
        except Exception as exc:
            logger.error("Failed to load trade history: {}", exc)

    async def _save_trade(self, record: dict[str, Any]) -> None:
        """Append a single trade record to JSONL file – O(1) write."""
        async with self._lock:
            try:
                with open(self.history_file, "a") as f:
                    f.write(json.dumps(record, default=str) + "\n")
                logger.debug("Trade appended to history ({} total)", len(self._trades))
            except Exception as exc:
                logger.error("Failed to append trade to history: {}", exc)

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
        if self._trades:
            last_win = self._trades[-1].get("is_win")
            streak = 0
            for t in reversed(self._trades):
                if t.get("is_win") == last_win:
                    streak += 1
                else:
                    break
            self._current_streak = streak if last_win else -streak

    def _rebuild_signal_stats(self) -> None:
        """Rebuild per-signal statistics from trade history."""
        self._signal_stats = {}
        for trade in self._trades:
            scoring = trade.get("scoring")
            if not scoring:
                continue
            vote_details = scoring.get("vote_details", {})
            is_win = trade.get("is_win", False)
            pnl = trade.get("pnl_usdt", 0.0)

            for signal_name, vote_value in vote_details.items():
                if vote_value == 0:
                    continue
                if signal_name not in self._signal_stats:
                    self._signal_stats[signal_name] = {
                        "trades": 0, "wins": 0, "losses": 0,
                        "total_pnl": 0.0, "total_win_pnl": 0.0,
                        "total_loss_pnl": 0.0,
                    }
                stats = self._signal_stats[signal_name]
                stats["trades"] += 1
                stats["total_pnl"] += pnl
                if is_win:
                    stats["wins"] += 1
                    stats["total_win_pnl"] += pnl
                else:
                    stats["losses"] += 1
                    stats["total_loss_pnl"] += pnl

        if self._signal_stats:
            logger.info("Rebuilt signal stats for {} signals", len(self._signal_stats))

    # ------------------------------------------------------------------
    # Record a closed trade (UPGRADED with signal attribution)
    # ------------------------------------------------------------------

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
        scoring: dict[str, Any] | None = None,  # NEW: ScoringResult.as_dict()
    ) -> dict[str, Any]:
        """Calculate P&L, record signal attribution, and persist.

        Parameters
        ----------
        scoring : dict, optional
            Output of ScoringResult.as_dict() — contains vote_details,
            weighted_breakdown, score, confidence, regime, etc.
        """
        now = datetime.now(timezone.utc)

        # P&L calculation WITH Binance fees (0.1% taker per side)
        entry_cost = entry_price * size
        exit_value = exit_price * size
        entry_fee = entry_cost * self.FEE_RATE
        exit_fee = exit_value * self.FEE_RATE
        total_fees = entry_fee + exit_fee

        if side.lower() == "long":
            pnl_usdt = (exit_value - entry_cost) - total_fees
        else:
            pnl_usdt = (entry_cost - exit_value) - total_fees

        pnl_pct = (pnl_usdt / entry_cost) * 100 if entry_cost > 0 else 0.0
        is_win = pnl_usdt > 0

        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "symbol": symbol,
            "side": side.lower(),
            "trade_type": trade_type.lower(),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "size": size,
            "fees_usdt": round(total_fees, 4),
            "pnl_usdt": round(pnl_usdt, 4),
            "pnl_before_fees": round(pnl_usdt + total_fees, 4),
            "pnl_pct": round(pnl_pct, 4),
            "is_win": is_win,
            "entry_time": entry_time or now.isoformat(),
            "exit_time": now.isoformat(),
            "reason": reason.lower(),
            # ===== NEW: Signal Attribution =====
            "scoring": scoring,  # Full scoring breakdown
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

        # ===== NEW: Update per-signal stats =====
        if scoring and "vote_details" in scoring:
            for signal_name, vote_value in scoring["vote_details"].items():
                if vote_value == 0:
                    continue
                if signal_name not in self._signal_stats:
                    self._signal_stats[signal_name] = {
                        "trades": 0, "wins": 0, "losses": 0,
                        "total_pnl": 0.0, "total_win_pnl": 0.0,
                        "total_loss_pnl": 0.0,
                    }
                stats = self._signal_stats[signal_name]
                stats["trades"] += 1
                stats["total_pnl"] += pnl_usdt
                if is_win:
                    stats["wins"] += 1
                    stats["total_win_pnl"] += pnl_usdt
                else:
                    stats["losses"] += 1
                    stats["total_loss_pnl"] += pnl_usdt

        await self._save_trade(record)

        logger.info(
            "Trade recorded | {} {} {} | entry={:.2f} exit={:.2f} | "
            "P&L={:+.2f} USDT ({:+.2f}%) | score={} | reason={} | total={}",
            trade_type.upper(), side.upper(), symbol,
            entry_price, exit_price, pnl_usdt, pnl_pct,
            scoring.get("score", "N/A") if scoring else "N/A",
            reason, len(self._trades),
        )
        return record

    # ------------------------------------------------------------------
    # NEW: EV (Expected Value) Tracking
    # ------------------------------------------------------------------

    def get_ev(self) -> dict[str, Any]:
        """Calculate overall Expected Value.

        EV = (win_rate x avg_win) - (loss_rate x avg_loss)

        Returns dict with EV per trade, total EV, and whether we have
        a statistical edge.
        """
        total = len(self._trades)
        if total == 0:
            return {"ev_per_trade": 0.0, "total_ev": 0.0, "has_edge": False, "sample_size": 0}

        wins = [t for t in self._trades if t.get("is_win")]
        losses = [t for t in self._trades if not t.get("is_win")]

        win_rate = len(wins) / total
        loss_rate = len(losses) / total
        avg_win = sum(t["pnl_usdt"] for t in wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(t["pnl_usdt"] for t in losses) / len(losses)) if losses else 0.0

        ev_per_trade = (win_rate * avg_win) - (loss_rate * avg_loss)
        total_ev = sum(t["pnl_usdt"] for t in self._trades)

        return {
            "ev_per_trade": round(ev_per_trade, 4),
            "total_ev": round(total_ev, 4),
            "has_edge": ev_per_trade > 0 and total >= 20,
            "sample_size": total,
            "min_sample_for_edge": 20,
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
        }

    # ------------------------------------------------------------------
    # NEW: Per-Signal Performance
    # ------------------------------------------------------------------

    def get_signal_performance(self) -> dict[str, dict[str, Any]]:
        """Get win rate and EV for each individual signal.

        Returns a dict keyed by signal name with stats for each.
        Useful for Phase 2 LearningEngine to adjust weights.
        """
        result = {}
        for signal_name, stats in self._signal_stats.items():
            trades = stats["trades"]
            if trades == 0:
                continue
            win_rate = stats["wins"] / trades
            avg_pnl = stats["total_pnl"] / trades
            result[signal_name] = {
                "trades": trades,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round(win_rate, 4),
                "avg_pnl": round(avg_pnl, 4),
                "total_pnl": round(stats["total_pnl"], 4),
                "ev_positive": avg_pnl > 0 and trades >= 10,
            }
        return result

    # ------------------------------------------------------------------
    # Original stats methods (preserved)
    # ------------------------------------------------------------------

    def get_daily_stats(self) -> dict[str, Any]:
        """Stats for trades closed today (UTC)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = [t for t in self._trades if t["exit_time"][:10] == today]
        return self._compute_stats(daily, label="daily")

    def get_session_stats(self) -> dict[str, Any]:
        """Stats since this bot process started."""
        return self._compute_stats(self._session_trades, label="session")

    def get_cumulative_stats(self) -> dict[str, Any]:
        """All-time stats across the full trade history."""
        stats = self._compute_stats(self._trades, label="cumulative")
        stats["longest_win_streak"] = self._longest_win_streak
        stats["longest_lose_streak"] = self._longest_lose_streak
        if self._current_streak > 0:
            stats["current_streak"] = f"{self._current_streak}W"
        elif self._current_streak < 0:
            stats["current_streak"] = f"{abs(self._current_streak)}L"
        else:
            stats["current_streak"] = "0"
        # ===== NEW: Add EV to cumulative stats =====
        stats["ev"] = self.get_ev()
        stats["signal_performance"] = self.get_signal_performance()
        return stats

    def reset_daily(self) -> None:
        """Called at UTC midnight."""
        self._daily_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        logger.info("TradeTrackerV2 daily reset – new date: {}", self._daily_date)

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
