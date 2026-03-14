# Alpha-Scalp Bot - Full Context Document (Updated)

One file to rule them all. Paste this entire file into any LLM to get full context of the bot without uploading individual files.

Repo: https://github.com/DevSiddh/alpha-scalp-bot

---

## 1. Project Overview

Alpha-Scalp Bot is an async Python quant trading bot for Binance Futures (USDT-M). It runs in live/demo mode using a real-time WebSocket pipeline (with REST polling fallback) and executes short-term scalp trades with an optional swing strategy overlay.

Core characteristics:
- Exchange: Binance Futures (fapi.binance.com / demo-fapi.binance.com)
- Default symbol: BTC/USDT (configurable)
- Primary timeframe: 1m candles (scalping)
- Secondary timeframe: 4h candles (swing overlay)
- Leverage: per-token configurable (TOKEN_LEVERAGE), default 5x
- Mode: DEMO (paper trading via Binance Demo Trading) and LIVE
- Signals: multi-factor voting system with dynamic LLM-optimized weights
- Risk: per-trade ATR-based SL/TP + time stop + daily circuit breaker + kill switch + max concurrent trades
- Notifications: Telegram alerts (HTML formatted) + /stats command + heartbeat every 30 min
- Backtesting: built-in backtester using exact production pipeline
- Startup: position reconciliation on restart (restores open positions, cancels orphan orders)

Tech stack:
- Python 3.11+
- ccxt >= 4.5.6 (Binance Demo Trading support via enable_demo_trading())
- websockets + aiohttp - real-time data streams
- pandas + pandas_ta - indicators
- loguru - logging (with SafeInterceptHandler for stdlib logging)
- httpx - async HTTP (Telegram, LLM API)

---

## 2. Architecture Diagram

```
[Binance Futures WS Streams]  (or REST polling fallback)
       |
       v
[BinanceWSManager] - kline_1m + depth@100ms + trade streams
       |
       v
[MarketState] - shared state (OHLCV ring buffer, order book, last trade)
       |
       v
[StateChangeDispatcher] - async PriorityQueue dispatcher (coalescing, backpressure)
       |
       v
[FeatureCache] - computes all TA indicators on OHLCV DataFrame (cached)
       |
       v
[AlphaEngine] - generates multi-signal votes (BUY/SELL/HOLD per signal)
       |
       v
[SignalScoring] - weighted score aggregation -> final BUY/SELL/HOLD + confidence
       |
       v
[RiskEngine] - ATR SL/TP, circuit breaker, Kelly sizing, kill switch, balance cache
       |
       v
[OrderExecutor] - ccxt order placement (market + SL/TP bracket), position monitor
       |
       v
[TradeTrackerV2] - JSONL trade log + signal attribution + session/cumulative stats
       |
       v
[TelegramAlerts] - entry/exit/heartbeat/daily-summary/stats alerts

[WeightOptimizer] - weekly LLM weight tuning from TradeTrackerV2 stats (profit factor gate)
[ScalpStrategy]   - premium NW-envelope + ADX + Kelly + session-filter signal engine
[SwingStrategy]   - parallel 4h EMA50/200 + RSI + S/R swing signal engine
[Backtester]      - offline PnL replay using FeatureCache -> AlphaEngine -> SignalScoring
```

Dual execution modes in main.py:
- Mode A (default): WebSocket event-driven (run_bot_ws)
- Mode B (fallback): REST polling loop (run_bot_polling)

---

## 3. File Index

| File | Purpose |
|---|---|
| main.py | Entry point. Dual-mode orchestrator (WS + polling). Position reconcile, swing loop, position monitor, time stop, weight optimizer trigger, /stats poller, graceful shutdown. |
| config.py | All constants and env-var loading (single source of truth for all parameters). |
| ws_manager.py | Binance WS manager: kline + depth@100ms + trade streams. Auto-reconnect, 23h keepalive, health monitoring. |
| market_state.py | Shared in-memory state: OHLCV ring buffer, order book (from WS diffs), last trade price, book health. |
| state_dispatcher.py | Async PriorityQueue dispatcher. 4 priority tiers, coalescing, backpressure. Fires PipelineCallbacks. |
| feature_cache.py | Computes EMA/RSI/MACD/BB/ATR/VWAP/OBV/VolMA on OHLCV. Caches result per candle. |
| alpha_engine.py | 9-signal voter (ema_cross, rsi_zone, macd_cross, bb_bounce, bb_squeeze, vwap_cross, obv_trend, volume_spike, swing_bias). |
| signal_scoring.py | Weighted score aggregation, regime detection, confidence gating. Loads weights.json. |
| risk_engine.py | ATR SL/TP, regime-adjusted sizing, Kelly, circuit breaker, kill switch, balance cache, swing risk methods. |
| order_executor.py | ccxt order execution (market + SL/TP bracket), get_position_info, close_position, cancel_all_orders, slippage model. |
| strategy.py | ScalpStrategy: NW kernel envelope + EMA9/21 + RSI + ADX + volume + BB squeeze + session filter + Kelly. |
| swing_strategy.py | SwingStrategy: 4h EMA50/200 + RSI zone + S/R pivot detection + volume + ATR. |
| trade_tracker_v2.py | JSONL trade persistence, signal attribution, session/cumulative stats, restore_open_position(). |
| weight_optimizer.py | LLM-based signal weight tuner. Profit factor gate (PF >= 1.2). Weekly cadence, 30 trade threshold. |
| telegram_alerts.py | Full alert suite: entry, exit, regime, circuit breaker, kill switch, heartbeat, daily summary, /stats, startup/shutdown. |
| backtest.py | PnL backtester: full production pipeline, ATR trailing stop, time stop, per-regime breakdown, equity curve. |
| weights.json | Default signal weights (overwritten by WeightOptimizer). |
| tests/test_bugs.py | Regression suite: 8 tests covering B1-B5. |
| requirements.txt | Python dependencies. |
| .env.example | Environment variable template. |

---

## 4. Core Configuration (config.py)

All settings loaded from environment variables with sensible defaults.

**Exchange:**
```
BINANCE_API_KEY         - Binance API key (Demo Trading keys from Binance Demo page)
BINANCE_SECRET          - Binance secret
BINANCE_DEMO_TRADING    - true/false (paper trade via demo-fapi.binance.com)
SYMBOL                  - default: BTC/USDT
TIMEFRAME               - default: 1m
LEVERAGE                - default: 5 (base leverage)
TOKEN_LEVERAGE          - per-token leverage override
LOOKBACK_CANDLES        - number of candles to fetch (REST polling)
USE_WEBSOCKET           - true/false (enables WS mode, default: true)
```

**WebSocket (Mode A only):**
```
WS_CANDLE_HISTORY       - candle history depth for MarketState ring buffer
WS_BOOK_DEPTH           - order book depth levels
WS_PRICE_JUMP_BPS       - basis points threshold to fire price_jump event
```

**Signal Thresholds:**
```
SIGNAL_THRESHOLD        - minimum confidence to open trade (e.g. 0.6)
MIN_WEIGHT              - minimum allowed signal weight (safety bound)
MAX_WEIGHT              - maximum allowed signal weight (safety bound)
```

**Risk Parameters:**
```
STOP_LOSS_PCT           - e.g. 0.005 (0.5%) base SL
TAKE_PROFIT_PCT         - e.g. 0.010 (1.0%) base TP
MAX_DAILY_LOSS_PCT      - daily circuit breaker threshold
MAX_CONCURRENT_TRADES   - max open positions at once
SCALP_MAX_HOLD_SECONDS  - time stop: force-exit after N seconds
SCALP_TRAIL_ACTIVATE_PCT - ATR trailing stop activation threshold
SCALP_TRAIL_DELTA_PCT   - trailing stop delta
POSITION_MONITOR_INTERVAL - seconds between position monitor checks
```

**Swing Strategy:**
```
SWING_ENABLED           - true/false
SWING_SYMBOLS           - list of symbols to trade swing
SWING_TIMEFRAME         - default: 4h
SWING_EMA_FAST          - default: 50
SWING_EMA_SLOW          - default: 200
SWING_RSI_PERIOD        - default: 14
SWING_RSI_LONG_LOW      - default: 40
SWING_RSI_LONG_HIGH     - default: 50
SWING_RSI_SHORT_LOW     - default: 50
SWING_RSI_SHORT_HIGH    - default: 60
SWING_LEVERAGE          - swing position leverage
SWING_LOOKBACK_CANDLES  - 4h candle history depth
SWING_CHECK_INTERVAL    - seconds between swing checks
```

**LLM Weight Optimizer:**
```
LLM_API_URL             - OpenAI-compatible endpoint
LLM_API_KEY             - API key
LLM_MODEL               - model name (e.g. gpt-4o)
```

**Telegram:**
```
TELEGRAM_BOT_TOKEN      - Telegram bot token
TELEGRAM_CHAT_ID        - chat/channel ID
STATS_COMMAND_ENABLED   - true/false (enables /stats polling)
```

**Persistence:**
```
TRADE_HISTORY_FILE      - path to JSONL trade log
LOOP_INTERVAL           - polling mode sleep interval (seconds)
```

---

## 5. WebSocket Manager (ws_manager.py)

Streams managed (Binance Futures fstream):
- `kline`: OHLCV candle updates (fires candle_complete on close)
- `depth@100ms`: order book diffs
- `trade`: real-time individual trades

Key features:
- Auto-reconnection with exponential backoff (initial=1s, max=60s, multiplier=2x)
- REST snapshot fallback for order book init + gap recovery
- 23-hour keepalive - proactively reconnects before Binance's 24h disconnect
- Graceful shutdown with full resource cleanup
- Health monitoring: `is_healthy` property, `metrics()` dict
- Staleness detection: warn at 10s, critical at 30s

Callbacks to StateChangeDispatcher:
- `on_kline` -> candle_complete (HIGH priority) or candle_update (NORMAL)
- `on_depth` -> book_update (LOW, coalesced 100ms)
- `on_trade` -> trade event

---

## 6. Market State (market_state.py)

Shared in-memory state passed to all WS callbacks.

Holds:
- `symbol` - trading pair
- `candles` - OHLCV ring buffer (history_len configurable)
- `book` - bid/ask depth (maintained from WS diffs), `initialized` flag, `spread_bps`, `imbalance`
- `last_trade_price` - most recent trade price
- `is_ready` - True when enough candles seeded AND book initialized
- `price_jump_threshold_bps` - configurable jump detection threshold

Key methods:
- `get_candle_df()` -> DataFrame of closed candles
- `get_book_snapshot()` -> dict with spread_bps, imbalance, best_bid, best_ask

---

## 7. State Dispatcher (state_dispatcher.py)

Full async event-driven pipeline dispatcher using `asyncio.PriorityQueue`.

**EventPriority enum:**
```
CRITICAL = 0   - book_invalidated, emergency halt
HIGH     = 10  - candle_complete, price_jump
NORMAL   = 20  - candle_update
LOW      = 30  - depth update (coalesced 100ms window)
```

**PipelineCallbacks dataclass (5 active callbacks):**
```
on_candle_complete   - full alpha pipeline trigger (candle close)
on_book_update       - spread/imbalance refresh
on_price_jump        - urgent re-score on abnormal price move
on_book_invalidated  - pause trading until book rebuilds
on_candle_update     - live (non-closed) candle tick
```

**Key behaviours:**
- Coalescing: depth updates coalesced within 100ms window
- Backpressure: LOW/NORMAL events dropped silently when queue full; CRITICAL/HIGH never dropped
- Pause: trading auto-pauses on `book_invalidated`; resumes on re-validation
- Metrics: `metrics()` -> dict (pipeline_runs, pipeline_errors, events_dropped, avg_pipeline_ms, queue_depth)

StateChangeDispatcher constructor:
```python
StateChangeDispatcher(
    state=market_state,
    callbacks=callbacks,
    queue_maxsize=1000,
    poll_interval_ms=50.0,
    coalesce_window_ms=100.0,
    max_processing_time_s=5.0,
)
```

**Bug B5 fixed:** queue overflow now drops the INCOMING low-priority event instead of draining existing high-priority events.

---

## 8. Feature Cache (feature_cache.py)

Computes all technical indicators on OHLCV DataFrame. Caches result per candle close.

**Indicators computed:**
```
ema_fast     - EMA (period from config)
ema_slow     - EMA (period from config)
rsi          - RSI 14
macd         - MACD 12/26/9 (line)
macd_signal  - MACD signal
macd_hist    - MACD histogram
bb_upper     - Bollinger Upper 20/2std
bb_mid       - Bollinger Mid
bb_lower     - Bollinger Lower
bb_squeeze   - bool: squeeze active
atr          - ATR 14
vwap         - VWAP session
obv          - OBV
vol_ma       - Volume MA 20
close        - latest close price
```

**Output:** `FeatureSet` dataclass with all above fields + raw DataFrame reference.
**Cache invalidation:** recomputes only when new candle data arrives.

---

## 9. Alpha Engine (alpha_engine.py)

Multi-signal voter. Each signal independently votes BUY / SELL / HOLD.

**Signals (9 total):**

| Signal | BUY Condition | SELL Condition |
|---|---|---|
| ema_cross | ema_fast crosses above ema_slow | ema_fast crosses below ema_slow |
| rsi_zone | RSI < 35 (oversold) | RSI > 65 (overbought) |
| macd_cross | MACD line crosses above signal | MACD line crosses below signal |
| bb_bounce | Price at/below bb_lower | Price at/above bb_upper |
| bb_squeeze | Squeeze ends + price moves up | Squeeze ends + price moves down |
| vwap_cross | Price crosses above VWAP | Price crosses below VWAP |
| obv_trend | OBV slope positive (N bars) | OBV slope negative (N bars) |
| volume_spike | Volume > 2x vol_ma (confirms) | Volume > 2x vol_ma (confirms) |
| swing_bias | 4h EMA golden cross + RSI zone | 4h EMA death cross + RSI zone |

**Vote interface (frozen):**
```python
Vote(direction: str, strength: float, reason: str)
# direction: "BUY" | "SELL" | "HOLD"
# strength: 0.0 to 1.0
```

**Output:** `VoteSet` - dict of `{signal_name: Vote}`

---

## 10. Signal Scoring (signal_scoring.py + weights.json)

Aggregates votes -> final signal with confidence + regime detection.

**Scoring logic:**
1. For each vote: `weighted_score = vote.strength * weight[signal_name]`
2. Sum BUY scores and SELL scores separately
3. `confidence = abs(bull_score - bear_score) / total_score`
4. Direction = whichever side has higher weighted score
5. If `confidence < SIGNAL_THRESHOLD` -> HOLD

**Regime detection:**
```
TRENDING_UP    - ema_fast > ema_slow, RSI 50-70
TRENDING_DOWN  - ema_fast < ema_slow, RSI 30-50
RANGING        - bb_squeeze active, EMAs flat
VOLATILE       - ATR > 2x average ATR
```

**weights.json defaults:**
```json
{
  "ema_cross":    1.5,
  "rsi_zone":     1.2,
  "macd_cross":   1.3,
  "bb_bounce":    1.0,
  "bb_squeeze":   1.1,
  "vwap_cross":   1.0,
  "obv_trend":    0.8,
  "volume_spike": 0.9,
  "swing_bias":   1.4
}
```
WeightOptimizer overwrites this file periodically (subject to profit factor gate).

**Output (frozen interface):**
```python
ScoredSignal(direction, confidence, regime, contributors, score_breakdown)
# also exposes: .action ("BUY"|"SELL"|"HOLD"), .score (float), .as_dict()
```

---

## 11. Risk Engine (risk_engine.py)

Guards all trade decisions. Methods used throughout main.py:

**Pre-trade checks:**
- `can_open_trade()` -> `(bool, reason_str)` - circuit breaker + max concurrent
- `check_kill_switch()` -> bool - hard stop (cumulative loss threshold)
- `get_stop_loss(entry, side, atr, regime)` -> float - ATR + regime-adjusted SL
- `get_take_profit(entry, side, atr, regime)` -> float - ATR + regime-adjusted TP
- `calculate_position_size(entry, sl)` -> float - risk-based sizing

**Confidence-scaled sizing (in main.py):**
```python
base_size = risk.calculate_position_size(entry, sl)
confidence_scale = 0.5 + (result.confidence * 0.5)  # 50%-100% of base
size = round(base_size * confidence_scale, 6)
```

**Kelly position sizing:**
```
f* = (win_rate * avg_win - loss_rate * avg_loss) / avg_win
position_size = equity * f* * KELLY_FRACTION_CAP
```

**Daily circuit breaker:**
```
if daily_pnl < -MAX_DAILY_LOSS_PCT * equity: block all new trades until UTC midnight
```

**Kill switch:** hard cumulative stop, triggers send_kill_switch_alert() + 12x sleep backoff.

**Balance cache:** `invalidate_balance_cache()` called after every order to force fresh balance fetch.

**Swing-specific methods:**
- `check_swing_max_positions(symbols)` -> bool
- `check_swing_total_exposure()` -> bool
- `check_swing_symbol_position(symbol)` -> bool (True = already have position)
- `get_swing_stop_loss(entry, side, symbol, atr)` -> float
- `get_swing_take_profit(entry, side, symbol)` -> float
- `calculate_swing_position_size(entry, sl)` -> float

**Output (frozen interface):**
```python
RiskDecision(allowed: bool, reason: str, position_size: float, kelly_fraction: float)
```

---

## 12. Order Executor (order_executor.py)

Places orders on Binance Futures via ccxt. Manages bracket orders and position lifecycle.

**Order flow (open):**
1. `set_leverage(symbol, leverage)` via ccxt
2. `set_margin_type(symbol)` - ISOLATED margin
3. Place MARKET order for calculated size
4. Apply slippage model to get expected fill price
5. Place STOP_MARKET (SL) order
6. Place TAKE_PROFIT_MARKET (TP) order
7. Return order dict with `_fill_price`, `_actual_sl`, `_actual_tp` keys

**Order flow (close):**
1. `close_position(symbol)` - market close
2. `cancel_all_orders(symbol)` - cancel residual SL/TP
3. Return order dict with `_exit_price`

**Position query:**
- `get_position_info(symbol)` -> dict `{side, entry_price, contracts}` or None

**Slippage model (P1-3):**
- Applied to fill price estimates
- Accounts for market impact on entry/exit

**FatalExchangeError:**
- Raised on auth/permission failures (ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.ExchangeNotAvailable)
- Propagated to main loop -> triggers graceful shutdown

---

## 13. Main Entry Point (main.py)

Orchestrates all components in two modes.

**Startup sequence (both modes):**
1. `_create_exchange()` - ccxt Binance client, connectivity ping (3 retries), Demo Trading via `enable_demo_trading(True)` if configured
2. Instantiate: RiskEngine, OrderExecutor, TelegramAlerts, TradeTrackerV2, FeatureCache, AlphaEngine, SignalScoring, WeightOptimizer
3. `reconcile_positions()` - P0-1: restore open positions, cancel orphan orders
4. `set_margin_type()` + `set_leverage()` for scalp symbol (and swing symbols)
5. `send_startup_message()`
6. Start `/stats` command poller task

**Stdlib logging interception:**
```python
class _SafeInterceptHandler(logging.Handler):
    # Routes stdlib logging -> loguru, swallows %-format mismatches from ccxt/websockets
```

**Mode A - WebSocket (run_bot_ws):**
- Creates `MarketState` + `BinanceWSManager` + `StateChangeDispatcher`
- Registers `PipelineCallbacks`: on_candle_complete, on_book_update, on_price_jump, on_book_invalidated
- `on_candle_complete`: full alpha pipeline + swing check + position monitor + weight optimizer
- `on_price_jump`: re-scores and trades only if confidence >= 0.8
- `on_book_update`: logs wide spreads (> 50 bps)
- `on_book_invalidated`: pauses trading + sends Telegram alert
- Keep-alive loop: 30s timeout, WS health check, 30-min heartbeat alert
- Midnight reset in both candle callback AND keep-alive loop (belt-and-suspenders)

**Mode B - Polling (run_bot_polling):**
- Fetches OHLCV via REST every `_TF_INTERVALS[TIMEFRAME]` seconds (1m=5s, 3m=15s, 5m=25s)
- Same alpha pipeline + swing + position monitor + weight optimizer
- `asyncio.wait_for(shutdown_event.wait(), timeout=loop_sleep)` for clean sleep

**Position monitor (both modes):**
- Runs every `POSITION_MONITOR_INTERVAL` seconds
- Tracks `known_positions: dict[symbol, {side, entry_price, contracts, entry_time}]`
- Detects: position disappeared (SL/TP hit by exchange) -> records trade + sends close alert
- Detects: new position appeared -> adds to known_positions
- Detects: time stop -> force-closes scalp if held > `SCALP_MAX_HOLD_SECONDS`
- Exit reason inferred: `"tp"` if price moved favorably, `"sl"` otherwise

**Swing trading loop (both modes):**
- Runs every `SWING_CHECK_INTERVAL` seconds
- Iterates `SWING_SYMBOLS`, skips if already have position for that symbol
- Checks `check_swing_max_positions()` and `check_swing_total_exposure()` before loop
- Fetches 4h OHLCV, runs SwingStrategy, executes if BUY/SELL
- Sends `send_swing_trade_alert()` on fill

**Weight optimizer trigger:**
- Weekly cadence (86400 * 7 seconds)
- Requires `len(tracker._trades) >= 30`
- Calls `optimizer.run_optimization_cycle()` -> reloads `signal_scoring._load_weights()`

**Graceful shutdown:**
- SIGINT / SIGTERM caught via `signal.signal()`
- Sets `shutdown_event`
- Stops dispatcher + WS manager (WS mode)
- Closes all open positions + cancels all orders (scalp + swing)
- Sends `send_shutdown_message()`

**FatalExchangeError propagation:**
- Auth/permission/maintenance errors in fetch_ohlcv() raise FatalExchangeError
- Main loop catches -> sends error alert -> sets shutdown_event -> breaks loop

**Telegram /stats command:**
- `_stats_poller()` task polls Telegram getUpdates every 5s
- Responds to `/stats` from configured chat_id with `tracker.get_session_stats()` + `get_cumulative_stats()`

---

## 14. ScalpStrategy (strategy.py)

Full premium multi-factor signal engine. Class: `ScalpStrategy`

**Signal components:**
1. **EMA 9/21 crossover** - primary trend direction
2. **RSI 14 momentum filter** - confirms momentum before entry
3. **Nadaraya-Watson (NW) Gaussian kernel envelope** - mean-reversion context
   - Non-parametric smoothed price envelope (mid, upper, lower bands)
   - BUY: bounce UP from NW lower (`prev_close < nw_lower AND curr_close > nw_lower`) <- B3 fixed
   - SELL: rejection DOWN from NW upper (`prev_close > nw_upper AND curr_close < nw_upper`)
4. **Volume spike** - volume > 1.5x 20-period SMA required
5. **Bollinger Band squeeze breakout** - low volatility compression -> expansion
6. **ADX regime detection** - trending vs ranging
   - ADX >= 25: trending (favours EMA cross + momentum)
   - ADX < 25: ranging (favours NW envelope mean-reversion)
   - ADX >= 40: strong trend
7. **Kelly Criterion position sizing** - running win/loss R:R stats via `update_kelly_stats()` <- B4 fixed
8. **Session filter** (P1-5) - dead zones block trades, active sessions boost confidence

**Session filter details:**
```
Dead zones (no trades):
  21:00-23:59 UTC (pre-close low liquidity)
  00:00-05:30 UTC (Asian overnight)
  Funding windows: XX:50 at 00:00, 08:00, 16:00 UTC (+/- 10 min)

Active sessions (1.0x confidence):
  08:00-10:30 UTC (London open)
  13:30-16:30 UTC (NY open overlap)
  14:30-17:30 UTC (NY afternoon)

Outside active but not dead: 0.8x confidence multiplier
```

**MarketRegime enum:** `TRENDING | RANGING | VOLATILE`

**TradeSignal dataclass fields:**
```python
signal, confidence, entry_price,
ema_fast, ema_slow, rsi,
nw_mid, nw_upper, nw_lower,
reason, atr, regime, adx,
volume_ratio, bb_squeeze, kelly_fraction
```

---

## 15. SwingStrategy (swing_strategy.py)

Higher-timeframe (4h) directional bias. Class: `SwingStrategy`

**Signals:**
1. **EMA 50/200 Golden/Death Cross** - primary trend
2. **RSI Zone Filter:**
   - BUY: RSI in [SWING_RSI_LONG_LOW=40, SWING_RSI_LONG_HIGH=50]
   - SELL: RSI in [SWING_RSI_SHORT_LOW=50, SWING_RSI_SHORT_HIGH=60]
3. **Support/Resistance** - pivot window=5 bars, lookback=50 bars
   - resistance = mean of top 3 recent swing highs
   - support = mean of bottom 3 recent swing lows
4. **Volume confirmation** - above-average volume required
5. **ATR** - dynamic SL sizing

**Key method:** `calculate_signals(df: DataFrame, symbol: str) -> SwingTradeSignal`

**SwingTradeSignal fields:**
```python
signal (SwingSignal.BUY|SELL|HOLD), confidence, entry_price,
ema_fast, ema_slow, rsi, support, resistance, reason, atr
```

**SwingSignal enum:** `BUY | SELL | HOLD` (`.value` = "BUY"/"SELL"/"HOLD")

---

## 16. Trade Tracker V2 (trade_tracker_v2.py)

Records all trades with signal attribution. JSONL persistence.

**Key additions over original TradeTracker:**
- Signal attribution (which signals contributed)
- Session vs cumulative stats split
- `restore_open_position()` for startup reconciliation (P0-1)
- `get_session_stats()` + `get_cumulative_stats()` for /stats command

**JSONL record format (frozen - never modify existing fields):**
```json
{
  "trade_id": "uuid",
  "symbol": "BTC/USDT",
  "side": "BUY|SELL",
  "trade_type": "scalp|swing",
  "entry_price": 0.0,
  "exit_price": 0.0,
  "sl": 0.0,
  "tp": 0.0,
  "size": 0.0,
  "pnl_usdt": 0.0,
  "pnl_pct": 0.0,
  "is_win": true,
  "exit_reason": "tp|sl|time_stop|trail|end",
  "regime": "string",
  "entry_time": "ISO",
  "exit_time": "ISO",
  "duration_s": 0,
  "scoring": {}
}
```

**Key methods:**
- `record_trade(symbol, side, trade_type, entry_price, exit_price, size, reason, entry_time, scoring)` -> dict
- `restore_open_position(symbol, side, entry_price, contracts, sl_price, tp_price)` - P0-1 startup
- `get_session_stats()` -> dict (trades/pnl since bot start)
- `get_cumulative_stats()` -> dict (all-time from JSONL)
- `get_signal_performance()` -> per-signal win rates and EV

**Fee rate:** `FEE_RATE = 0.001` (0.1% taker, 0.2% round trip)

---

## 17. Weight Optimizer (weight_optimizer.py)

LLM-based agentic feedback loop. Class: `WeightOptimizer`

**How it works:**
1. `get_cumulative_stats()` from TradeTrackerV2
2. `_check_profit_factor_gate()` - blocks update if PF < 1.2
3. Builds strict prompt with per-signal win rates + EV
4. LLM responds with updated weights JSON
5. Validates against [MIN_WEIGHT, MAX_WEIGHT]
6. Saves to `weights.json`
7. `signal_scoring._load_weights()` picks up new weights

**Profit Factor Gate:**
```
if profit_factor < 1.2: freeze weights, send Telegram alert
if trade_count < 10:    skip gate (insufficient data)
```

**Bug fixes:**
- B1: `asyncio.get_running_loop().create_task()` in try/except RuntimeError
- B2: `aiohttp.ClientTimeout(total=30)` object, not scalar

**LLM config:** temperature=0.1, OpenAI-compatible API.

**Key method:** `run_optimization_cycle()` -> bool (success)

---

## 18. Telegram Alerts (telegram_alerts.py)

Full alert suite via Bot API (HTML formatting).

**Alert types:**
```
send_startup_message()                          - bot started
send_shutdown_message(reason)                   - bot stopped
send_trade_alert(side, symbol, entry_price,     - scalp entry
    stop_loss, take_profit, size, leverage,
    strategy, regime, confidence, atr_value)
send_swing_trade_alert(side, symbol, entry,     - swing entry
    sl, tp, size, confidence, reason)
send_close_alert(side, symbol, entry_price,     - position closed
    exit_price, pnl, pnl_pct, strategy,
    exit_reason)
send_daily_summary(pnl, trades, win_rate,       - midnight reset
    start_balance, end_balance, cumulative_stats)
send_stats(session_stats, cumulative_stats)     - /stats command reply
send_heartbeat(uptime_str, last_signal,         - every 30 min
    last_score, regime, book_ok, total_trades,
    session_pnl, cumulative_trades,
    cumulative_pnl, spread_bps)
send_kill_switch_alert()                        - kill switch triggered
send_circuit_breaker(daily_pnl)                 - daily loss limit hit
send_error_alert(exc)                           - unhandled / fatal error
send_message(text)                              - generic send
get_updates(offset, timeout)                    - poll for /stats commands
```

**Rate limiting:** 1.0s minimum between messages (asyncio lock).
**Fallback:** HTML parse fail (400) -> retry as plain text.

---

## 19. Backtest (backtest.py)

Full PnL backtester using exact production pipeline.

**Pipeline:** `FeatureCache.compute(df)` -> `AlphaEngine.generate_votes(fs)` -> `SignalScoring.score(votes, fs)`

**Simulates:**
- Configurable SL/TP (from config.py)
- Time stop (`SCALP_MAX_HOLD_SECONDS` converted to 1m bars)
- ATR trailing stop (`SCALP_TRAIL_ACTIVATE_PCT` + `SCALP_TRAIL_DELTA_PCT`)
- Slippage: `SLIPPAGE_PCT = 0.0003` applied to entry/exit
- Optional fees: `--fees` flag adds 0.04% maker fee per side

**Trade dataclass fields:**
```python
bar_in, ts_in, side, entry, sl, tp, confidence, regime, contributors
bar_out, ts_out, exit_price, exit_reason, pnl_pct, pnl_net
```

**Data source:** OKX REST (avoids Binance geo-restrictions in sandbox)

**CLI:**
```bash
python backtest.py                    # 2000 bars
python backtest.py --bars 5000        # more history
python backtest.py --bars 5000 --fees # with maker fee
python backtest.py --symbol ETH/USDT  # different pair
```

**Output metrics:**
- Trades (W/L count), win rate, total PnL
- Profit factor, max drawdown, final equity
- Avg hold duration (bars)
- Per-exit-reason breakdown (tp/sl/trail/time_stop)
- Last 10 trades table
- Per-regime breakdown

---

## 20. End-to-End Data Flow

**WebSocket Mode (primary):**
```
1. WS kline arrives
   -> BinanceWSManager.on_kline() -> MarketState candle buffer updated

2. Candle closes (is_closed=True)
   -> StateChangeDispatcher enqueues DispatchEvent(priority=HIGH, "candle_complete")

3. Dispatcher worker fires on_candle_complete(state, meta)
   -> Midnight reset check
   -> Kill switch check -> skip if active
   -> state.get_candle_df() -> DataFrame
   -> FeatureCache.compute(df) -> FeatureSet
   -> AlphaEngine.generate_votes(features) -> VoteSet (9 signals)
   -> SignalScoring.score(votes, features) -> ScoredSignal

4. If ScoredSignal.action in ("BUY", "SELL"):
   -> ATR SL/TP from RiskEngine
   -> confidence_scale = 0.5 + (confidence * 0.5)
   -> size = base_size * confidence_scale
   -> risk.can_open_trade() -> gate check
   -> OrderExecutor.open_position() -> market + SL/TP bracket
   -> risk.invalidate_balance_cache()
   -> alerts.send_trade_alert()

5. Swing check (if time since last >= SWING_CHECK_INTERVAL):
   -> For each SWING_SYMBOL not already in position:
      -> fetch_swing_ohlcv() -> 4h DataFrame
      -> SwingStrategy.calculate_signals() -> SwingTradeSignal
      -> If BUY/SELL: risk sizing -> execute -> swing alert

6. Position monitor (if time since last >= POSITION_MONITOR_INTERVAL):
   -> For each monitored symbol:
      -> get_position_info() vs known_positions
      -> Disappeared: record_trade() + close_alert (SL/TP hit)
      -> New: add to known_positions
      -> Still open + scalp + held > SCALP_MAX_HOLD_SECONDS: TIME STOP

7. Weight optimizer (weekly, 30 trades):
   -> optimizer.run_optimization_cycle()
   -> signal_scoring._load_weights()

8. Price jump event (on_price_jump):
   -> Re-score, trade only if confidence >= 0.8

9. WS keep-alive loop (every 30s):
   -> ws_manager.is_healthy check
   -> dispatcher.metrics() log
   -> Every 30 min: send_heartbeat() with uptime, last signal, regime, PnL
   -> Midnight reset (belt-and-suspenders)
```

---

## 21. Known Bugs - Status

| Bug | Description | Status |
|---|---|---|
| B1 | asyncio.get_event_loop().create_task() in sync context | FIXED: get_running_loop() + try/except RuntimeError |
| B2 | aiohttp timeout=30 scalar had no effect | FIXED: aiohttp.ClientTimeout(total=30) |
| B3 | NW lower band long_cross condition inverted | FIXED: prev_close < nw_lower AND curr_close > nw_lower |
| B4 | Kelly update_kelly_stats() skipped _total_losses_r accumulation | FIXED: loss branch now adds to _total_losses_r |
| B5 | Priority queue overflow drained existing high-priority events | FIXED: drops incoming low-priority event instead |

---

## 22. Phase Completion Tracker

| Phase | Feature | Status |
|---|---|---|
| B1-B5 | Bug fixes | DONE |
| P0-1 | Position reconcile on restart | DONE (reconcile_positions() in main.py) |
| P0-2 | Config defaults (all params env-var driven) | DONE |
| P1-1 | Token profiles (per-token leverage) | DONE (TOKEN_LEVERAGE in config) |
| P1-2 | ATR-based SL/TP | DONE (risk.get_stop_loss/get_take_profit) |
| P1-3 | Slippage model | DONE (order_executor.py) |
| P1-4 | Volatility filter | DONE (VOLATILE regime in SignalScoring) |
| P1-5 | Session filter | DONE (ScalpStrategy session dead zones + active sessions) |
| P1-6 | Regime disabling | DONE (regime-based signal gating in AlphaEngine/ScalpStrategy) |
| P1-10 | Drawdown leverage + profit factor gate | DONE (WeightOptimizer PF gate) |

---

## 23. Frozen Interfaces (NEVER modify signatures)

```python
# Vote
Vote(direction: str, strength: float, reason: str)

# FeatureSet dataclass - only ADD fields, never remove
# (current fields: ema_fast, ema_slow, rsi, macd, macd_signal, macd_hist,
#  bb_upper, bb_mid, bb_lower, bb_squeeze, atr, vwap, obv, vol_ma, close, df)

# ScoredSignal
ScoredSignal(direction, confidence, regime, contributors)
# Also exposes: .action, .score, .as_dict()

# RiskDecision
RiskDecision(allowed: bool, reason: str, position_size: float, kelly_fraction: float)

# TradeTrackerV2 JSONL format - only ADD fields
```

---

## 24. DO NOT TOUCH Files

These files must never be opened or modified:
```
state_dispatcher.py
weight_optimizer.py
ws_manager.py
market_state.py
trade_tracker_v2.py
telegram_alerts.py
```

---

## 25. Dependencies

**requirements.txt:**
```
ccxt>=4.5.6          # must be >= 4.5.6 for Binance Demo Trading
pandas>=2.0.0
pandas-ta>=0.3.14b
numpy>=1.24.0
loguru>=0.7.0
python-dotenv>=1.0.0
httpx>=0.25.0
websockets
aiohttp
```

**.env.example:**
```
BINANCE_API_KEY=your_demo_trading_key
BINANCE_SECRET=your_demo_trading_secret
BINANCE_DEMO_TRADING=true
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
SYMBOL=BTC/USDT
TIMEFRAME=1m
LEVERAGE=5
USE_WEBSOCKET=true
LOG_LEVEL=INFO
```

**Setup:**
```bash
git clone https://github.com/DevSiddh/alpha-scalp-bot
cd alpha-scalp-bot
pip install -r requirements.txt   # ensure ccxt >= 4.5.6
cp .env.example .env
# Generate Demo Trading API keys from:
# https://www.binance.com/en/support/faq/detail/9be58f73e5e14338809e3b705b9687dd
python main.py
```

---

## 26. Test Suite (tests/test_bugs.py)

**Run:** `pytest tests/test_bugs.py -v`
**Result:** 8/8 pass

| Test | Bug | What it verifies |
|---|---|---|
| test_b1_get_running_loop | B1 | asyncio.get_running_loop() used; no RuntimeError in async context |
| test_b1_no_event_loop_error | B1 | fire-and-forget task creation does not raise in sync fallback |
| test_b2_aiohttp_timeout_object | B2 | aiohttp.ClientTimeout(total=30) instance used, not scalar |
| test_b3_nw_lower_band_long_cross | B3 | long_cross fires on prev<lower AND curr>lower |
| test_b3_nw_lower_band_no_false_fire | B3 | long_cross does NOT fire when price stays below nw_lower |
| test_b4_kelly_loss_accumulation | B4 | update_kelly_stats() accumulates _total_losses_r on loss |
| test_b4_kelly_win_accumulation | B4 | update_kelly_stats() accumulates _total_wins_r on win |
| test_b5_queue_overflow_drops_incoming | B5 | low-priority incoming event dropped on queue full |

---

## 27. Prompt Template for AI Refinement

```
SYSTEM ROLE: Expert async Python crypto futures bot developer.
Bot: Alpha-Scalp Bot | Binance Futures USDT-M | Python 3.11 | TF: 1m

GROUND RULES:
1. Read a file ONLY when you need to edit it.
2. Write tests BEFORE fixing bugs.
3. Never re-read a file you already read this session.
4. Never summarise files back to me.
5. Output ONLY complete changed files. No snippets.
6. After all code is done, run pytest once. Report pass/fail count only.
7. Do NOT update PROJECT_STATUS.md unless explicitly asked.

DO NOT TOUCH:
state_dispatcher.py, weight_optimizer.py, ws_manager.py,
market_state.py, trade_tracker_v2.py, telegram_alerts.py

FROZEN INTERFACES:
Vote(direction, strength, reason)
FeatureSet dataclass fields - only ADD, never remove
ScoredSignal(direction, confidence, regime, contributors)
RiskDecision(allowed, reason, position_size, kelly_fraction)
TradeTrackerV2 JSONL format

ALREADY DONE:
B1-B5, P0-1, P0-2, P1-1 through P1-6, P1-10

[PASTE CONTEXT.md HERE]

TASK: [DESCRIBE YOUR SPECIFIC IMPROVEMENT REQUEST]

Constraints:
- Maintain existing async architecture
- FeatureSet backward compatible (add-only)
- New signals must follow Vote(direction, strength, reason)
- New config params must be in config.py + .env.example
- Preserve TradeTrackerV2 JSONL format
```

---

Generated by Nebula AI - https://nebula.gg
Last updated: 2026-03-14
Repo: https://github.com/DevSiddh/alpha-scalp-bot
