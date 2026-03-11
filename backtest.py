"""Alpha-Scalp Bot – Backtest Script.

Uses the full alpha engine pipeline:
    FeatureCache.compute(df) → AlphaEngine.generate_votes(fs) → SignalScoring.score(votes, fs)

Replaces the old ScalpStrategy.calculate_signals(df) path.
"""

import ccxt
import pandas as pd
from loguru import logger

from feature_cache import FeatureCache
from alpha_engine import AlphaEngine
from signal_scoring import SignalScoring

# ── Fetch OHLCV data ────────────────────────────────────────────────
# OKX used instead of Binance to avoid sandbox geo-restrictions (HTTP 451)
exchange = ccxt.okx()
raw = exchange.fetch_ohlcv("BTC/USDT", "1m", limit=2000)
df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
df["timestamp"] = pd.to_datetime(df["ts"], unit="ms")
df.set_index("timestamp", inplace=True)
df = df[["open", "high", "low", "close", "volume"]].astype(float)

# ── Initialise pipeline components ──────────────────────────────────
cache = FeatureCache()
engine = AlphaEngine()
scorer = SignalScoring()  # loads weights.json or defaults

# ── Run backtest ────────────────────────────────────────────────────
MIN_BARS = 100  # need enough history for indicators (EMA, ATR, etc.)
signals: list[tuple] = []

for i in range(MIN_BARS, len(df)):
    window = df.iloc[: i + 1]

    # 1. Compute features
    try:
        features = cache.compute(window)
    except Exception as exc:
        logger.warning("FeatureCache error at bar {}: {}", i, exc)
        continue

    # 2. Generate alpha votes
    votes = engine.generate_votes(features)

    # 3. Score and decide
    result = scorer.score(votes, features)

    if result.action != "HOLD":
        signals.append((
            df.index[i],
            result.action,
            result.confidence,
            result.score,
            result.regime,
            result.contributing_signals,
        ))

# ── Print results ───────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"Backtest complete: {len(signals)} signals from {len(df)-MIN_BARS} bars")
print(f"Signal rate: {len(signals)/(len(df)-MIN_BARS)*100:.1f}%")
print(f"{'='*60}")

if signals:
    buys = sum(1 for s in signals if s[1] == "BUY")
    sells = len(signals) - buys
    print(f"BUY: {buys} | SELL: {sells}")
    avg_conf = sum(s[2] for s in signals) / len(signals)
    print(f"Avg confidence: {avg_conf:.1%}")
    print(f"\nLast 10 signals:")
    print(f"{'Time':<22} {'Side':<5} {'Conf':>6} {'Score':>7} {'Regime':<10} Contributors")
    print("-" * 80)
    for ts, side, conf, score, regime, contribs in signals[-10:]:
        print(f"{str(ts):<22} {side:<5} {conf:>5.1%} {score:>+7.2f} {regime:<10} {', '.join(contribs)}")
else:
    print("No signals generated — check thresholds or data quality.")
