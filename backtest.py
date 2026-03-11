"""Alpha-Scalp Bot – Full PnL Backtest.

Uses the production alpha engine pipeline:
    FeatureCache.compute(df) -> AlphaEngine.generate_votes(fs) -> SignalScoring.score(votes, fs)

Simulates trades with:
    - Configurable SL / TP (from config.py)
    - Time stop (SCALP_MAX_HOLD_SECONDS)
    - ATR trailing stop activation + delta lock
    - Per-trade PnL, equity curve, and summary statistics

Usage:
    python backtest.py                        # default 2000 bars
    python backtest.py --bars 5000            # more history
    python backtest.py --bars 5000 --fees     # include 0.04% maker fee
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import ccxt
import pandas as pd
from loguru import logger

import config as cfg
from feature_cache import FeatureCache
from alpha_engine import AlphaEngine
from signal_scoring import SignalScoring


# ── Trade record ────────────────────────────────────────────────────
@dataclass
class Trade:
    bar_in: int
    ts_in: object  # pd.Timestamp
    side: str  # BUY | SELL
    entry: float
    sl: float
    tp: float
    confidence: float
    regime: str
    contributors: list[str] = field(default_factory=list)
    # filled on exit
    bar_out: int = 0
    ts_out: object = None
    exit_price: float = 0.0
    exit_reason: str = ""  # tp | sl | time_stop | trail
    pnl_pct: float = 0.0
    pnl_net: float = 0.0  # after fees


# ── Backtest engine ─────────────────────────────────────────────────
class Backtester:
    def __init__(self, df: pd.DataFrame, fee_pct: float = 0.0):
        self.df = df
        self.fee_pct = fee_pct  # one-way fee (applied on entry + exit)

        # Pipeline
        self.cache = FeatureCache()
        self.engine = AlphaEngine()
        self.scorer = SignalScoring()

        # Config
        self.sl_pct = cfg.STOP_LOSS_PCT
        self.tp_pct = cfg.TAKE_PROFIT_PCT
        self.max_hold_bars = max(1, int(cfg.SCALP_MAX_HOLD_SECONDS / 60))  # convert seconds to 1m bars
        self.trail_activate_pct = cfg.SCALP_TRAIL_ACTIVATE_PCT
        self.trail_delta_pct = getattr(cfg, 'SCALP_TRAIL_DELTA_PCT', 0.002)

        # State
        self.trades: list[Trade] = []
        self.position: Trade | None = None
        self.equity = 1000.0  # notional starting equity
        self.equity_curve: list[float] = []

    def _calc_sl_tp(self, entry: float, side: str) -> tuple[float, float]:
        if side == "BUY":
            sl = entry * (1 - self.sl_pct)
            tp = entry * (1 + self.tp_pct)
        else:
            sl = entry * (1 + self.sl_pct)
            tp = entry * (1 - self.tp_pct)
        return sl, tp

    def _check_exit(self, bar_idx: int, row) -> str | None:
        """Check if current bar triggers an exit. Returns reason or None."""
        pos = self.position
        if pos is None:
            return None

        high, low, close = row["high"], row["low"], row["close"]

        # 1. Stop loss hit?
        if pos.side == "BUY" and low <= pos.sl:
            return "sl"
        if pos.side == "SELL" and high >= pos.sl:
            return "sl"

        # 2. Take profit hit?
        if pos.side == "BUY" and high >= pos.tp:
            return "tp"
        if pos.side == "SELL" and low <= pos.tp:
            return "tp"

        # 3. Trailing stop check
        if pos.side == "BUY":
            move_pct = (high - pos.entry) / pos.entry
            if move_pct >= self.trail_activate_pct:
                trail_stop = pos.entry * (1 + self.trail_delta_pct)
                if low <= trail_stop:
                    return "trail"
        else:
            move_pct = (pos.entry - low) / pos.entry
            if move_pct >= self.trail_activate_pct:
                trail_stop = pos.entry * (1 - self.trail_delta_pct)
                if high >= trail_stop:
                    return "trail"

        # 4. Time stop
        bars_held = bar_idx - pos.bar_in
        if bars_held >= self.max_hold_bars:
            return "time_stop"

        return None

    def _close_position(self, bar_idx: int, row, reason: str) -> None:
        pos = self.position
        ts = self.df.index[bar_idx]

        # Determine exit price based on reason
        if reason == "sl":
            exit_price = pos.sl
        elif reason == "tp":
            exit_price = pos.tp
        elif reason == "trail":
            if pos.side == "BUY":
                exit_price = pos.entry * (1 + self.trail_delta_pct)
            else:
                exit_price = pos.entry * (1 - self.trail_delta_pct)
        else:  # time_stop
            exit_price = row["close"]

        # PnL
        if pos.side == "BUY":
            pnl_pct = (exit_price - pos.entry) / pos.entry
        else:
            pnl_pct = (pos.entry - exit_price) / pos.entry

        # Subtract fees (entry + exit)
        pnl_net = pnl_pct - (self.fee_pct * 2)

        # Update trade record
        pos.bar_out = bar_idx
        pos.ts_out = ts
        pos.exit_price = exit_price
        pos.exit_reason = reason
        pos.pnl_pct = pnl_pct
        pos.pnl_net = pnl_net

        # Update equity
        self.equity *= (1 + pnl_net)

        self.trades.append(pos)
        self.position = None

    def run(self) -> None:
        MIN_BARS = 100
        logger.info("Starting backtest: {} bars, fee={:.4%}", len(self.df), self.fee_pct)

        for i in range(MIN_BARS, len(self.df)):
            row = self.df.iloc[i]

            # Check exit first (if in position)
            if self.position is not None:
                reason = self._check_exit(i, row)
                if reason:
                    self._close_position(i, row, reason)

            # Only look for entries if flat
            if self.position is None:
                window = self.df.iloc[: i + 1]
                try:
                    features = self.cache.compute(window)
                    votes = self.engine.generate_votes(features)
                    result = self.scorer.score(votes, features)
                except Exception as exc:
                    logger.debug("Pipeline error at bar {}: {}", i, exc)
                    self.equity_curve.append(self.equity)
                    continue

                if result.action != "HOLD":
                    entry = row["close"]
                    sl, tp = self._calc_sl_tp(entry, result.action)
                    self.position = Trade(
                        bar_in=i,
                        ts_in=self.df.index[i],
                        side=result.action,
                        entry=entry,
                        sl=sl,
                        tp=tp,
                        confidence=result.confidence,
                        regime=result.regime,
                        contributors=result.contributing_signals[:],
                    )

            self.equity_curve.append(self.equity)

        # Force close any open position at end
        if self.position is not None:
            last_row = self.df.iloc[-1]
            self._close_position(len(self.df) - 1, last_row, "end")

    def print_summary(self) -> None:
        n = len(self.trades)
        if n == 0:
            print("\nNo trades generated. Check thresholds or data quality.")
            return

        wins = [t for t in self.trades if t.pnl_net > 0]
        losses = [t for t in self.trades if t.pnl_net <= 0]
        total_pnl = sum(t.pnl_net for t in self.trades)
        gross_profit = sum(t.pnl_net for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl_net for t in losses)) if losses else 0.001

        # Max drawdown from equity curve
        peak = self.equity_curve[0] if self.equity_curve else 1000
        max_dd = 0.0
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

        # Exit reason breakdown
        reasons = {}
        for t in self.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

        # Avg hold time in bars
        avg_hold = sum(t.bar_out - t.bar_in for t in self.trades) / n

        print(f"\n{'='*65}")
        print(f"  BACKTEST RESULTS  ({len(self.df)} bars, fee={self.fee_pct:.4%}/side)")
        print(f"{'='*65}")
        print(f"  Trades      : {n}  (W:{len(wins)} / L:{len(losses)})")
        print(f"  Win Rate    : {len(wins)/n:.1%}")
        print(f"  Total PnL   : {total_pnl:+.2%}")
        print(f"  Profit Fctr : {gross_profit/gross_loss:.2f}")
        print(f"  Max Drawdown: {max_dd:.2%}")
        print(f"  Final Equity: {self.equity:.2f}  (from 1000.00)")
        print(f"  Avg Hold    : {avg_hold:.1f} bars")
        print(f"  Exit Reasons: {reasons}")
        print(f"{'='*65}")

        # Last 10 trades
        print(f"\n  Last 10 trades:")
        print(f"  {'Time':<20} {'Side':<5} {'Entry':>10} {'Exit':>10} {'PnL':>8} {'Reason':<10} {'Regime':<10}")
        print(f"  {'-'*78}")
        for t in self.trades[-10:]:
            print(
                f"  {str(t.ts_in)[:19]:<20} {t.side:<5} "
                f"{t.entry:>10.2f} {t.exit_price:>10.2f} "
                f"{t.pnl_net:>+7.2%} {t.exit_reason:<10} {t.regime:<10}"
            )

        # Per-regime breakdown
        regimes = {}
        for t in self.trades:
            r = t.regime
            if r not in regimes:
                regimes[r] = {"n": 0, "wins": 0, "pnl": 0.0}
            regimes[r]["n"] += 1
            regimes[r]["pnl"] += t.pnl_net
            if t.pnl_net > 0:
                regimes[r]["wins"] += 1

        print(f"\n  Per-regime breakdown:")
        print(f"  {'Regime':<12} {'Trades':>7} {'Win%':>7} {'PnL':>9}")
        print(f"  {'-'*38}")
        for r, s in sorted(regimes.items()):
            wr = s["wins"] / s["n"] if s["n"] else 0
            print(f"  {r:<12} {s['n']:>7} {wr:>6.1%} {s['pnl']:>+8.2%}")


# ── Main ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Alpha-Scalp Backtest")
    parser.add_argument("--bars", type=int, default=2000, help="Number of 1m candles")
    parser.add_argument("--fees", action="store_true", help="Include 0.04%% maker fee")
    parser.add_argument("--symbol", type=str, default="BTC/USDT", help="Trading pair")
    args = parser.parse_args()

    fee = 0.0004 if args.fees else 0.0

    # Fetch data (OKX avoids Binance sandbox geo-restrictions)
    logger.info("Fetching {} bars of {} from OKX...", args.bars, args.symbol)
    exchange = ccxt.okx()
    raw = exchange.fetch_ohlcv(args.symbol, "1m", limit=args.bars)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    logger.info("Loaded {} candles: {} to {}", len(df), df.index[0], df.index[-1])

    bt = Backtester(df, fee_pct=fee)
    bt.run()
    bt.print_summary()


if __name__ == "__main__":
    main()
