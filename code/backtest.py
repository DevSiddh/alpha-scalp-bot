"""Alpha-Scalp Bot – Full PnL Backtest.

Uses the production alpha engine pipeline:
    FeatureCache.compute(df) -> AlphaEngine.generate_votes(fs) -> SignalScoring.score(votes, fs)

FIXED (2026-03-14):
- TIMEFRAME hardcoded to 3m (OKX data source)
- SIGNAL_THRESHOLD=0.60, SCORE_THRESHOLD=1.8 for backtest mode
- LOOKBACK_CANDLES=200 minimum
- --debug flag: prints every vote, score, rejection reason, regime per candle
- Formatted BACKTEST RESULTS output block with regime breakdown
- Regime distribution log after run completes

Usage:
    python code/backtest.py --bars 500
    python code/backtest.py --bars 500 --fees
    python code/backtest.py --bars 500 --fees --debug
    python code/backtest.py --bars 1000 --symbol ETH/USDT
"""

from __future__ import annotations

import argparse
import sys
import os
from dataclasses import dataclass, field
from collections import defaultdict

import ccxt
import pandas as pd
from loguru import logger

# ── backtest must run from repo root so imports resolve ──────────────────────
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_CODE_DIR)
sys.path.insert(0, _REPO_ROOT)   # config.py, feature_cache.py, etc. live here
sys.path.insert(0, _CODE_DIR)    # alpha_engine.py, signal_scoring.py live here

import config as cfg
from feature_cache import FeatureCache
from alpha_engine import AlphaEngine
from signal_scoring import SignalScoring

# ── Backtest-specific overrides ──────────────────────────────────────────────
TIMEFRAME        = "3m"          # ISSUE 2: hardcoded 3m
SIGNAL_THRESHOLD = 0.60          # ISSUE 2: lower than live 0.72
SCORE_THRESHOLD  = 1.8           # Lower than production 2.0 to validate signals
CONSENSUS_THR    = 0.52          # Slightly more permissive than live 0.55
LOOKBACK_CANDLES = 200           # ISSUE 2: minimum 200
FEE_PCT_DEFAULT  = 0.0004        # 0.04% maker fee (OKX)


# ── Trade record ─────────────────────────────────────────────────────────────
@dataclass
class Trade:
    bar_in:      int
    ts_in:       object
    side:        str          # BUY | SELL
    entry:       float
    sl:          float
    tp:          float
    confidence:  float
    regime:      str
    contributors: list[str] = field(default_factory=list)
    bar_out:     int   = 0
    ts_out:      object = None
    exit_price:  float = 0.0
    exit_reason: str   = ""   # tp | sl | time_stop | trail | end
    pnl_pct:     float = 0.0
    pnl_net:     float = 0.0


# ── Backtester ────────────────────────────────────────────────────────────────
class Backtester:
    SLIPPAGE_PCT: float = 0.0003

    def __init__(self, df: pd.DataFrame, fee_pct: float = 0.0, debug: bool = False):
        self.df      = df
        self.fee_pct = fee_pct
        self.debug   = debug

        # Pipeline — backtest-mode thresholds
        self.cache  = FeatureCache()
        self.engine = AlphaEngine()
        self.scorer = SignalScoring(
            score_threshold=SCORE_THRESHOLD,
            consensus_threshold=CONSENSUS_THR,
        )

        # Config
        self.sl_pct             = cfg.STOP_LOSS_PCT
        self.tp_pct             = cfg.TAKE_PROFIT_PCT
        self.max_hold_bars      = max(1, int(cfg.SCALP_MAX_HOLD_SECONDS / (3 * 60)))  # 3m bars
        self.trail_activate_pct = cfg.SCALP_TRAIL_ACTIVATE_PCT
        self.trail_delta_pct    = getattr(cfg, "SCALP_TRAIL_DELTA_PCT", 0.002)

        # State
        self.trades: list[Trade]    = []
        self.position: Trade | None = None
        self.equity                 = 1000.0
        self.equity_curve: list[float] = []
        self.regime_counts: dict[str, int] = defaultdict(int)
        self._rejected_reasons: list[str]  = []

    def _calc_sl_tp(self, entry: float, side: str) -> tuple[float, float]:
        if side == "BUY":
            return entry * (1 - self.sl_pct), entry * (1 + self.tp_pct)
        return entry * (1 + self.sl_pct), entry * (1 - self.tp_pct)

    def _check_exit(self, bar_idx: int, row) -> str | None:
        pos  = self.position
        high, low = row["high"], row["low"]

        if pos.side == "BUY":
            if low  <= pos.sl: return "sl"
            if high >= pos.tp: return "tp"
            move = (high - pos.entry) / pos.entry
            if move >= self.trail_activate_pct:
                if low <= pos.entry * (1 + self.trail_delta_pct):
                    return "trail"
        else:
            if high >= pos.sl: return "sl"
            if low  <= pos.tp: return "tp"
            move = (pos.entry - low) / pos.entry
            if move >= self.trail_activate_pct:
                if high >= pos.entry * (1 - self.trail_delta_pct):
                    return "trail"

        if (bar_idx - pos.bar_in) >= self.max_hold_bars:
            return "time_stop"
        return None

    def _close_position(self, bar_idx: int, row, reason: str) -> None:
        pos = self.position
        ts  = self.df.index[bar_idx]

        if reason == "sl":
            exit_price = pos.sl
        elif reason == "tp":
            exit_price = pos.tp
        elif reason == "trail":
            if pos.side == "BUY":
                exit_price = pos.entry * (1 + self.trail_delta_pct) * (1 - self.SLIPPAGE_PCT)
            else:
                exit_price = pos.entry * (1 - self.trail_delta_pct) * (1 + self.SLIPPAGE_PCT)
        else:  # time_stop / end
            if pos.side == "BUY":
                exit_price = row["close"] * (1 - self.SLIPPAGE_PCT)
            else:
                exit_price = row["close"] * (1 + self.SLIPPAGE_PCT)

        if pos.side == "BUY":
            pnl_pct = (exit_price - pos.entry) / pos.entry
        else:
            pnl_pct = (pos.entry - exit_price) / pos.entry

        pnl_net = pnl_pct - (self.fee_pct * 2)

        pos.bar_out    = bar_idx
        pos.ts_out     = ts
        pos.exit_price = exit_price
        pos.exit_reason = reason
        pos.pnl_pct    = pnl_pct
        pos.pnl_net    = pnl_net

        self.equity *= (1 + pnl_net)
        self.trades.append(pos)
        self.position = None

    def run(self) -> None:
        MIN_BARS = max(LOOKBACK_CANDLES, 100)
        logger.info(
            "Backtest start: {} bars | fee={:.4%} | timeframe={} | "
            "score_thresh={} | consensus_thresh={}",
            len(self.df), self.fee_pct, TIMEFRAME, SCORE_THRESHOLD, CONSENSUS_THR,
        )

        for i in range(MIN_BARS, len(self.df)):
            row = self.df.iloc[i]

            # Track regime distribution
            # (computed after feature extraction below; pre-fill with last known)
            # Exit check first
            if self.position is not None:
                reason = self._check_exit(i, row)
                if reason:
                    self._close_position(i, row, reason)

            if self.position is None:
                window = self.df.iloc[max(0, i - LOOKBACK_CANDLES): i + 1]
                try:
                    features = self.cache.compute(window)
                    self.regime_counts[features.regime] += 1

                    if self.debug:
                        logger.info(
                            "[BAR {}] regime={} close={:.2f} rsi={:.1f} "
                            "ema_fast={:.2f} ema_slow={:.2f} atr={:.2f} atr_ratio={:.2f}",
                            i, features.regime, features.close, features.rsi,
                            features.ema_fast, features.ema_slow,
                            features.atr, features.atr / features.atr_ma50 if features.atr_ma50 > 0 else 0,
                        )

                    votes  = self.engine.generate_votes(features, debug=self.debug)
                    result = self.scorer.score(votes, features, debug=self.debug)

                    if self.debug and result.action == "HOLD":
                        reason_str = "vol_filter" if result.volatility_filter_triggered else \
                                     f"score={result.score:+.2f}<{SCORE_THRESHOLD}"
                        logger.info("[BAR {}] REJECTED: {}", i, reason_str)
                        self._rejected_reasons.append(reason_str)

                except Exception as exc:
                    logger.debug("Pipeline error at bar {}: {}", i, exc)
                    self.equity_curve.append(self.equity)
                    continue

                if result.action != "HOLD":
                    raw_entry = row["close"]
                    if result.action == "BUY":
                        entry = raw_entry * (1 + self.SLIPPAGE_PCT)
                    else:
                        entry = raw_entry * (1 - self.SLIPPAGE_PCT)
                    sl, tp = self._calc_sl_tp(entry, result.action)
                    self.position = Trade(
                        bar_in      = i,
                        ts_in       = self.df.index[i],
                        side        = result.action,
                        entry       = entry,
                        sl          = sl,
                        tp          = tp,
                        confidence  = result.confidence,
                        regime      = result.regime,
                        contributors= result.contributing_signals[:],
                    )
                    if self.debug:
                        logger.info(
                            "[BAR {}] TRADE {} | entry={:.2f} sl={:.2f} tp={:.2f} "
                            "conf={:.1%} regime={}",
                            i, result.action, entry, sl, tp,
                            result.confidence, result.regime,
                        )

            self.equity_curve.append(self.equity)

        # Force-close any open position
        if self.position is not None:
            last_row = self.df.iloc[-1]
            self._close_position(len(self.df) - 1, last_row, "end")

        # ISSUE 3: regime distribution
        logger.info("Regime distribution: {}", dict(self.regime_counts))

        if self.debug and self._rejected_reasons:
            from collections import Counter
            reason_counts = Counter(self._rejected_reasons)
            logger.info("[DEBUG] Rejection breakdown: {}", dict(reason_counts))

    # ── ISSUE 4: Formatted results output ────────────────────────────────────
    def print_summary(self, symbol: str = "BTCUSDT") -> None:
        n = len(self.trades)
        SEP = "\u2501" * 48

        print(f"\n{SEP}")
        print(f"  BACKTEST RESULTS \u2014 {symbol} {TIMEFRAME}")
        print(SEP)

        if n == 0:
            print("  Bars tested:      {:>6}".format(len(self.df)))
            print("  Trades generated:      0")
            print("\n  No trades generated. Check thresholds or data quality.")
            print(f"  Regime distribution: {dict(self.regime_counts)}")
            print(SEP)
            return

        wins   = [t for t in self.trades if t.pnl_net > 0]
        losses = [t for t in self.trades if t.pnl_net <= 0]
        total_pnl    = sum(t.pnl_net for t in self.trades)
        gross_profit = sum(t.pnl_net for t in wins)         if wins   else 0.0
        gross_loss   = abs(sum(t.pnl_net for t in losses))  if losses else 0.001

        # Max drawdown
        peak   = self.equity_curve[0] if self.equity_curve else 1000.0
        max_dd = 0.0
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

        avg_hold = sum(t.bar_out - t.bar_in for t in self.trades) / n

        # Exit reasons
        reasons: dict[str, int] = {}
        for t in self.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

        print("  Bars tested:      {:>6}".format(len(self.df)))
        print("  Trades generated: {:>6}".format(n))
        print("  Win rate:         {:>6.1%}".format(len(wins) / n))
        print("  Profit factor:    {:>6.2f}".format(gross_profit / gross_loss))
        print("  Max drawdown:     {:>6.1%}".format(max_dd))
        print("  Total PnL:        {:>+7.1%}".format(total_pnl))
        print("  Avg hold (bars):  {:>6.1f}".format(avg_hold))
        print(SEP)
        print("  Win trades:  {:>4}".format(len(wins)))
        print("  Loss trades: {:>4}".format(len(losses)))
        print("  TP hits:     {:>4}".format(reasons.get("tp", 0)))
        print("  SL hits:     {:>4}".format(reasons.get("sl", 0)))
        print("  Time stops:  {:>4}".format(reasons.get("time_stop", 0)))
        print("  Trail stops: {:>4}".format(reasons.get("trail", 0)))
        print(SEP)

        # Regime breakdown
        regime_trade_counts: dict[str, int] = {}
        for t in self.trades:
            regime_trade_counts[t.regime] = regime_trade_counts.get(t.regime, 0) + 1

        print("  Regime breakdown:")
        for r in ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "VOLATILE"]:
            count = regime_trade_counts.get(r, 0)
            print("  {:14s} {:>4} trades".format(r + ":", count))
        print(SEP)

        # Regime distribution (candle-level)
        print("  Regime distribution (candles):")
        for r, cnt in sorted(self.regime_counts.items(), key=lambda x: -x[1]):
            print("  {:14s} {:>5} candles".format(r + ":", cnt))
        print(SEP)

        # Last 10 trades
        print("\n  Last 10 trades:")
        print("  {:20s} {:4s} {:>10} {:>10} {:>8} {:10s} {:10s}".format(
            "Time", "Side", "Entry", "Exit", "PnL", "Reason", "Regime"))
        print("  " + "-" * 78)
        for t in self.trades[-10:]:
            print(
                "  {:20s} {:4s} {:>10.2f} {:>10.2f} {:>+7.2%} {:10s} {:10s}".format(
                    str(t.ts_in)[:19], t.side,
                    t.entry, t.exit_price, t.pnl_net,
                    t.exit_reason, t.regime,
                )
            )
        print()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Alpha-Scalp Backtest (3m / OKX)")
    parser.add_argument("--bars",   type=int,   default=500,         help="Number of 3m candles to fetch")
    parser.add_argument("--fees",   action="store_true",             help="Include 0.04% maker fee")
    parser.add_argument("--symbol", type=str,   default="BTC/USDT",  help="Trading pair")
    parser.add_argument("--debug",  action="store_true",             help="Print every vote, score, rejection reason")
    args = parser.parse_args()

    fee    = FEE_PCT_DEFAULT if args.fees else 0.0
    symbol = args.symbol

    # ISSUE 5: --debug sets loguru to DEBUG level
    if args.debug:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG",
                   format="{time:HH:mm:ss} | {level:<7} | {message}")
        logger.info("[DEBUG MODE ON] verbose output enabled")
    else:
        logger.remove()
        logger.add(sys.stderr, level="INFO",
                   format="{time:HH:mm:ss} | {level:<7} | {message}")

    # ISSUE 2: OKX as data source, 3m timeframe
    logger.info("Fetching {} bars of {} {} from OKX...", args.bars, symbol, TIMEFRAME)
    try:
        exchange = ccxt.okx({"enableRateLimit": True})
        raw = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=args.bars)
    except Exception as e:
        logger.error("Failed to fetch data from OKX: {}", e)
        sys.exit(1)

    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    logger.info("Loaded {} candles: {} to {}", len(df), df.index[0], df.index[-1])

    bt = Backtester(df, fee_pct=fee, debug=args.debug)
    bt.run()

    # Clean symbol for display (BTC/USDT -> BTCUSDT)
    display_symbol = symbol.replace("/", "")
    bt.print_summary(symbol=display_symbol)


if __name__ == "__main__":
    main()
