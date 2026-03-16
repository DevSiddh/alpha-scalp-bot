# Grand Prix Alpha-Scalp — CLAUDE.md

You are the lead engineer on my crypto futures scalping bot called
Grand Prix Alpha-Scalp. You have full context of the architecture.
Do not ask me to explain the system — you already know it completely.

════════════════════════════════════════════════════════════════
SYSTEM IDENTITY
════════════════════════════════════════════════════════════════

Bot name: Grand Prix Alpha-Scalp
Asset: BTC/USDT perpetual futures, Binance, 3-minute scalping
Leverage: 5x scalp, 3x swing
Fee model: 0.1% taker per side = 0.2% round trip (always deduct)
Language: Python 3.11
Testing: pytest, 102 tests currently passing (Steps 1–8 done)
Platform: Windows 11, VS Code, Claude as primary engineer

════════════════════════════════════════════════════════════════
COMPLETED STEPS (DO NOT TOUCH THESE)
════════════════════════════════════════════════════════════════

STEP 1 ✅ — weights.json frozen, WEIGHTS_LOCKED=true,
            WeightOptimizer removed from main.py loop

STEP 2 ✅ — RiskEngine extended:
            Three-Strike (3 losses → 90min cooldown)
            Equity floor (80% starting balance → shutdown)
            Active Cash Mode state (logged, not passive)
            Min SL floor: max(atr_sl, price × 0.0015)
            ATR min validation: raises ConfigError if < 1.8
            Regime-aware R:R: RANGING=1.5, TRENDING=2.0, VOLATILE=1.8
            Kelly disabled until 300 live trades, cap 10% full Kelly

STEP 3 ✅ — Quick wins batch:
            Spike filter in SignalScoring (>3×ATR → HOLD, 3-candle cooldown)
            Spread volatility guard (>2× 5min baseline → block)
            ATR min config validation on startup
            Max single signal weight cap at 2.0

STEP 4 ✅ — signal_registry.py built:
            Single declarative source of truth for all 11 signals
            Each entry has: family, role, default_weight,
            disable_in_regimes, sub_strategies, lagging flag
            AlphaEngine + SignalScoring both read from registry

STEP 5 ✅ — sub_strategy_manager.py built:
            5 sub-strategies defined with evaluate() method
            Microstructure hard gate enforced (trade_aggression
            OR ob_imbalance OR liquidity_sweep must agree)
            swing_bias hard gate enforced (4h signal blocks counter-trend)
            rsi_zone + bb_bounce mutual exclusion in Mean Reversion
            Cash Mode defined as Strategy 6

STEP 6 ✅ — shadow_tracker.py built:
            ShadowTracker simulates all 5 strategies every candle
            Fee deduction in simulated PnL (0.1% entry + 0.1% exit)
            Normalised expectancy reward with division-by-zero guard:
              if pnl_max == pnl_min: reward = 0.0
              else: reward = (pnl - pnl_min)/(pnl_max - pnl_min)*2 - 1
              reward = max(-1.0, min(1.0, reward))
            Beta(α,β) updated per (strategy, symbol) pair
            Rolling deque(maxlen=50) for correlation computation
            distinct_regimes_seen tracked per strategy
            shadow_trades.jsonl appended with regime, fee, weights_version
            TradeTrackerV2 extended with record_shadow_trade() method

════════════════════════════════════════════════════════════════
REMAINING STEPS TO BUILD
════════════════════════════════════════════════════════════════

STEP 7  ✅ — tournament_engine.py built:
            TournamentEngine — Thompson Sampling over ShadowTracker Betas
            CASH_SAMPLE_THRESHOLD = 0.50 (THOMPSON_NONE_THRESHOLD)
            Normalised expectancy min-max scaled to [0,1] per candle pool
            Equal-expectancy fallback → 0.5 for all (avoids negative values)
            HmmScheduler (FIX-5): initial train at 87k candles,
              Sunday retrain when new_candles > 500
            12 tests passing

STEP 8  ✅ — strategy_router.py built:
            StrategyRouter — promote/bench lifecycle management
            FIX-2: burn-in requires candles_seen >= 50 AND distinct_regimes >= 2
            Velocity check: win-rate drop > 0.30 per 10-trade window → bench
            Correlation check: Pearson ρ >= 0.85 → bench lower-ranked strategy
            Telegram alerts on all bench/promote events
            12 tests passing

STEP 9  — exit_engine.py (4-state machine + 4 regression tests)
STEP 10 — deepseek_pit_boss.py (audit-only, no weight writes)
STEP 11 — symbol_context.py (per-symbol isolation)
STEP 12 — portfolio_correlation_guard.py (RiskEngine final gate)
STEP 13 — ETH/SOL passive shadow + paper trading 2 weeks

════════════════════════════════════════════════════════════════
COMPONENT SHARING RULES (CRITICAL FOR STEPS 11-12)
════════════════════════════════════════════════════════════════

SymbolContext     NOT shared — one instance per symbol, fully isolated.
                  BTC SymbolContext never touches ETH SymbolContext.

AlphaEngine       SHARED — stateless, one instance, safe.
                  Called with different SymbolContext each candle.
                  Reads only, never writes state.

ShadowTracker     SHARED but keyed by (strategy, symbol) tuple.
                  ETH and BTC Beta distributions never mix.

RiskEngine        SHARED with split state:
                  daily_pnl and consecutive_losses → PER SYMBOL
                  equity_floor                     → GLOBAL (all symbols)
                  kill_switch                      → GLOBAL

PortfolioCorrelationGuard  SHARED by design — only component that
                  NEEDS cross-symbol access. Must see ALL open
                  positions across ALL symbols simultaneously.

ExitEngine        NOT shared — one instance PER OPEN POSITION.
                  Created on entry, destroyed on exit.
                  Never reused across trades or symbols.

════════════════════════════════════════════════════════════════
DATA FLOW — SINGLE TRADE END TO END
════════════════════════════════════════════════════════════════

 1. BinanceWSManager receives 3m candle close for BTC
 2. Routes to BTC SymbolContext
 3. FeatureCache computes indicators → FeatureSet
 4. OrderFlowCache updates rolling deques
 5. AlphaEngine computes → AlphaVotes
 6. SubStrategyManager evaluates → SubStrategySignals
 7. ShadowTracker simulates all 5 → updates Beta
 8. TournamentEngine samples → TournamentResult
 9. StrategyRouter checks promote/bench (every 20 candles)
10. If TournamentResult.action != CASH_MODE:
      → RiskEngine.can_open_trade() → 7 gates
      → If all pass: OrderExecutor places entry
      → ExitEngine instance created for this position
11. Every subsequent candle for open position:
      → ExitEngine.on_candle() → state machine update
      → If State 3: OrderExecutor places exit
      → TradeTrackerV2.record_trade()
      → TelegramAlerts.send()

════════════════════════════════════════════════════════════════
FULL ARCHITECTURE — 5 LAYERS
════════════════════════════════════════════════════════════════

Layer 1 — Data Ingestion
  BinanceWSManager    multi-symbol WebSocket routing
  SymbolContext       per-symbol state container (Step 11)
  FeatureCache        one instance per SymbolContext
  OrderFlowCache      per-symbol rolling trade deque

Layer 2 — Alpha Generation
  SignalRegistry      single signal metadata source (done)
  AlphaEngine         stateless, shared across symbols
  SubStrategyManager  5 sub-strategies + Cash Mode (done)

Layer 3 — Tournament
  ShadowTracker       Beta distributions (done)
  TournamentEngine    Thompson Sampling (Step 7)
  StrategyRouter      promote/bench (Step 8)

Layer 4 — Risk & Execution
  RiskEngine          9 gates (done)
  PortfolioCorrelationGuard  (Step 12)
  OrderExecutor       limit/market by regime
  ExitEngine          4-state machine (Step 9)

Layer 5 — Intelligence
  TradeTrackerV2      live + shadow records
  DeepSeekPitBoss     weekly audit (Step 10)
  TelegramAlerts      all alerts

════════════════════════════════════════════════════════════════
11 SIGNALS (DOWN FROM 17)
════════════════════════════════════════════════════════════════

PHASE 1 ACTIVE (7 — already in bot):
  bb_squeeze       core    volatility family
  vwap_cross       core    structure family
  liquidity_sweep  core    microstructure family
  trade_aggression core    microstructure family
  ob_imbalance     support microstructure family
  mtf_bias         support structure family
  funding_bias     support bias family

PHASE 2 (add after 200 live trades):
  kalman_signal    core    replaces EMA+NW
                          Q=0.001, R=0.01
  fvg_signal       support institutional family

PHASE 3 (add after 500 live trades):
  equal_highs_lows support institutional family
  llm_pattern      support contextual family

REMOVED PERMANENTLY:
  macd_cross, obv_trend, ema_cross, nw_signal,
  adx_filter, bb_bounce, volume_spike

════════════════════════════════════════════════════════════════
6 STRATEGIES
════════════════════════════════════════════════════════════════

1. Breakout
   Signals: bb_squeeze + volume_spike + trade_aggression
   Allowed regimes: TRENDING_UP, TRENDING_DOWN
   SL: ATR 1.8×, TP: tightening trail

2. VWAP Mean Reversion
   Signals: bb_bounce/rsi_zone (mutex) + vwap_cross
   Allowed regimes: RANGING, NEUTRAL
   SL: ATR 1.5×, TP: fixed 1.5×ATR

3. Liquidity Sweep Reversal
   Signals: liquidity_sweep + ob_imbalance + nw_signal
   Allowed regimes: ALL except VOLATILE
   SL: ATR 2.0×, TP: tightening trail

4. Trend Pullback
   Signals: mtf_bias + kalman_signal
   Allowed regimes: TRENDING_UP, TRENDING_DOWN
   SL: ATR 1.8×, TP: trail

5. Order Flow Momentum
   Signals: trade_aggression + ob_imbalance
   Allowed regimes: ALL
   SL: ATR 1.5×, TP: fixed 1.5×ATR

6. Cash Mode (Strategy 6)
   Trigger: Thompson score < 0.55 OR strategies disagree
   Tracked in ShadowTracker as real decision
   Logged to shadow_trades.jsonl with reason + duration

════════════════════════════════════════════════════════════════
5 HMM REGIME STATES
════════════════════════════════════════════════════════════════

TRENDING_UP | TRENDING_DOWN | RANGING | VOLATILE | TRANSITION

TRANSITION = ADX 18-25 AND ATR ratio 0.8-1.2 (uncertain zone)
In TRANSITION: only Order Flow Momentum allowed

HMM fallback: ADX-based regime if HMM unavailable
HMM initial training: 6 months historical BTC 3m (87k candles)
HMM retraining: every Sunday if new_candles > 500

════════════════════════════════════════════════════════════════
RISK GATES (6 clean gates in can_open_trade())
════════════════════════════════════════════════════════════════

1. kill_switch_check()          — 3% daily drawdown hard stop
2. three_strike_check()         — 3 losses → 90min pause
3. equity_floor_check()         — 80% balance → full shutdown
4. spread_guard()               — regime-adjusted max spread
5. kyles_lambda_check()         — adverse selection filter
6. max_positions_check()        — concurrent trades cap
+ portfolio_correlation_guard() — added in Step 12

════════════════════════════════════════════════════════════════
DYNAMIC LEVERAGE FORMULA
════════════════════════════════════════════════════════════════

REGIME CEILINGS:
  TRENDING: 5.0×   RANGING: 3.0×
  VOLATILE: 2.0×   TRANSITION: 2.0×

THOMPSON CONFIDENCE MULTIPLIER:
  score < 0.70  → 0.50× size
  score 0.70-0.85 → 0.75× size
  score > 0.85  → 1.00× size

NEGATIVE FUNDING BONUS:
  if funding < -0.01% AND direction == BUY:
    leverage_bonus = +1.0 (longs get paid)
  (only if raw_leverage < ceiling - 1.0)

DRAWDOWN SCALING:
  drawdown > 3.5% → 0.25× multiplier
  drawdown > 2.0% → 0.50× multiplier

THREE-STRIKE DE-RISK:
  consecutive_losses == 2 → 0.75× multiplier
  consecutive_losses == 3 → PAUSED (gate blocks entirely)

════════════════════════════════════════════════════════════════
MULTI-SYMBOL ACTIVATION RULES
════════════════════════════════════════════════════════════════

Every candle close → route_agent_activation() runs:

  BTC/USDT:  ALWAYS              → full pipeline
  ETH/USDT:  BTC == NEUTRAL      → full pipeline
             else                → shadow only
  SOL/USDT:  BTC == NEUTRAL
             AND ETH == NEUTRAL  → full pipeline
             else                → shadow only
  ALL NEUTRAL                    → Global Cash Mode

"Full pipeline" = SignalScoring → Tournament → Router → Risk → Execution
"Shadow only"   = ShadowTracker runs, no live orders placed

PASSIVE_SHADOW_SYMBOLS=ETH/USDT,SOL/USDT
  SymbolContext created on startup for all symbols
  ShadowTracker runs every candle regardless of activation state
  Builds Beta distributions before live activation (warm start)

Portfolio correlation guard:
  Block if Pearson r > 0.75 AND same direction
  Computed on rolling 50-candle return series

════════════════════════════════════════════════════════════════
EXIT ENGINE — 4-STATE MACHINE (Step 9)
════════════════════════════════════════════════════════════════

State 0 ENTRY
  Record: entry_price, entry_atr, regime_at_entry

State 1 BREAKEVEN
  Trigger: price hits SCALP_TRAIL_ACTIVATE_PCT (0.5%)
  Action: move SL to entry_price

State 2 TRAILING
  RANGING:  fixed limit TP at entry + 1.5×ATR, no trail
  TRENDING: trail at 2×ATR → 1×ATR at +5% → 0.75×ATR at +10%
  VOLATILE: if no profit after 4 candles → market exit
            if in profit → tight 1×ATR trail immediately

State 3 EXIT
  Log: exit_reason, exit_price, state_history, hold_duration

MANDATORY REGRESSION TESTS (all must pass before Step 9 complete):
  test_ranging_exit_hits_fixed_tp_not_trailing()
  test_trending_trail_tightens_at_5pct_profit()
  test_volatile_time_exit_triggers_at_candle_4()
  test_breakeven_state_transitions_correctly()

════════════════════════════════════════════════════════════════
KEY CRITICAL FIXES (already implemented in Steps 1-6)
════════════════════════════════════════════════════════════════

FIX-1: Division-by-zero guard in normalised expectancy ✅
FIX-2: Burn-in raised to 50 candles + 2 distinct regimes ✅
FIX-3: Shadow PnL always deducts 0.2% round-trip fee ✅
FIX-4: ExitEngine 4 regression tests (implement in Step 9)
FIX-5: HMM trained on 6mo historical BTC before first run
FIX-6: Kalman Q=0.001, R=0.01
FIX-7: Hypothesis semantic overlap check (> 70% = reject)
FIX-8: Negative funding rate increases leverage for longs
FIX-9: Paper slippage model = realistic (not zero)
FIX-10: All symbols NEUTRAL → global cash mode

════════════════════════════════════════════════════════════════
PERSISTENCE ARCHITECTURE (survives VPS crash + LLM switch)
════════════════════════════════════════════════════════════════

bot_state.json          — written atomically after EVERY state change
                          (write to .tmp first, then os.replace())
trades.jsonl            — all live closed trades
shadow_trades.jsonl     — all shadow simulation records (rotates at 50MB)
loss_audit_log.jsonl    — all losing trades with signal context
weights_v1.json         — frozen base weights
weights_active.json     — pointer to active version

STARTUP RECONCILIATION (runs before any trading):
  1. Load bot_state.json
  2. Fetch live positions from exchange
  3. Reconcile: missing = closed while down, mismatch = partial fill
  4. Restore Beta distributions
  5. Restore cooldown states
  6. Restore/reset daily PnL counters
  7. Send "Bot restarted — reconciliation complete" Telegram

Telegram backup: key files auto-sent every 5 minutes AND on trade close
Cost: ₹0 (already have bot token)

════════════════════════════════════════════════════════════════
COMPLETE .ENV KEYS
════════════════════════════════════════════════════════════════

BINANCE_API_KEY=
BINANCE_SECRET=
BINANCE_DEMO_TRADING=false
USE_WEBSOCKET=true
SCALP_SYMBOLS=BTC/USDT
PASSIVE_SHADOW_SYMBOLS=ETH/USDT,SOL/USDT
TIMEFRAME=3m
LEVERAGE=5
SWING_SYMBOLS=BTC/USDT
SWING_TIMEFRAME=4h
SWING_LEVERAGE=3
GRAND_PRIX_ENABLED=true
BURN_IN_CANDLES=50
DISTINCT_REGIMES_REQUIRED=2
VELOCITY_CHECK_THRESHOLD=0.30
CORRELATION_BLOCK_THRESHOLD=0.80
THOMPSON_NONE_THRESHOLD=0.55
WEIGHTS_LOCKED=true
WEIGHTS_VERSION=v1
MIN_SL_DISTANCE_PCT=0.15
SPIKE_FILTER_ATR_MULT=3.0
SPIKE_COOLDOWN_CANDLES=3
FUNDING_BIAS_MAX_WEIGHT=0.5
MAX_SINGLE_SIGNAL_WEIGHT=2.0
KALMAN_Q=0.001
KALMAN_R=0.01
RISK_PER_TRADE=0.02
DAILY_DRAWDOWN_LIMIT=0.03
DAILY_LOSS_LIMIT=0.02
MAX_OPEN_POSITIONS=3
MAX_CONCURRENT_TRADES=5
RR_RANGING=1.5
RR_TRENDING=2.0
RR_VOLATILE=1.8
ATR_SL_MULTIPLIER=2.0
ATR_SL_MULTIPLIER_MIN=1.8
ATR_TP_MULTIPLIER=3.0
ATR_RATIO_MIN=0.5
ATR_RATIO_MAX=3.0
THREE_STRIKE_ENABLED=true
THREE_STRIKE_COOLDOWN_MINUTES=90
EQUITY_FLOOR_PCT=0.80
KELLY_ENABLED=true
KELLY_MAX_FRACTION=0.10
KELLY_MIN_TRADES=300
KELLY_RAMP_TRADES=500
SCALP_MAX_LEVERAGE=5
SWING_MAX_LEVERAGE=3
LEVERAGE_CEILING_TRENDING=5.0
LEVERAGE_CEILING_RANGING=3.0
LEVERAGE_CEILING_VOLATILE=2.0
LEVERAGE_CEILING_TRANSITION=2.0
CONFIDENCE_MULT_LOW=0.50
CONFIDENCE_MULT_MID=0.75
CONFIDENCE_MULT_HIGH=1.00
FUNDING_LEVERAGE_BONUS=1.0
FUNDING_BONUS_THRESHOLD=-0.0001
ATR_TRAIL_ENABLED=true
SCALP_TRAIL_ACTIVATE_PCT=0.005
SCALP_TRAIL_ATR_MULT=2.0
TRAIL_TIGHTEN_AT_5PCT=1.0
TRAIL_TIGHTEN_AT_10PCT=0.75
PORTFOLIO_CORRELATION_THRESHOLD=0.75
USE_LIMIT_ENTRY_IN_RANGING=true
SPREAD_MULT_RANGING=1.0
SPREAD_MULT_TRENDING=1.5
SPREAD_MULT_VOLATILE=0.75
PARTIAL_FILL_TOLERANCE=0.05
PITBOSS_ENABLED=true
PITBOSS_AUDIT_DAY=Sunday
LLM_API_URL=https://api.deepseek.com/v1/chat/completions
LLM_API_KEY=
LLM_MODEL=deepseek-chat
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
PAPER_TRADING_MODE=false
PAPER_SLIPPAGE_MODEL=realistic
INITIAL_BALANCE=100
TRADE_HISTORY_FILE=trades.jsonl
FUNDING_EXTREME_LONG_THRESHOLD=-0.0003
FUNDING_EXTREME_SHORT_THRESHOLD=0.0005
FUNDING_STRATEGY_ENABLED=true

════════════════════════════════════════════════════════════════
LLM COST BREAKDOWN
════════════════════════════════════════════════════════════════

DeepSeek V3 pricing: $0.03/M input, $0.08/M output

Pattern Recognition: $0.041/month
Regime Narrator:     $0.003/month
Loss Auditor:        $0.007/month
Pit Boss Weekly:     $0.001/month
Total LLM:           $0.052/month = ₹4/month

VPS:     ₹400/month
Total:   ₹404/month operational cost
Capital: ₹50,000+ recommended for economic viability

════════════════════════════════════════════════════════════════
YOUR ROLE AS ENGINEER
════════════════════════════════════════════════════════════════

When I ask you to build a step:
  1. Write complete, production-ready Python code
  2. Include all imports
  3. Match existing code style and naming conventions
  4. Write pytest tests for every new method
  5. Flag any design decision I need to make
  6. Never rewrite completed steps unless I explicitly ask
  7. Ask for existing code if you need to match signatures

When I paste an error:
  1. Diagnose the exact cause first
  2. Give the minimal fix — do not rewrite the whole file
  3. Explain why it failed in one sentence

When I ask a design question:
  1. Give your recommendation with reasoning
  2. Name tradeoffs clearly
  3. Keep answers short — I'm building, not reading essays

Current test status: 102 tests passing
Current step: Ready for Step 9 — ExitEngine (hardest step, do in fresh session)

════════════════════════════════════════════════════════════════
MODEL SELECTION POLICY
════════════════════════════════════════════════════════════════

Use the right model for each task — do not waste Sonnet on simple work.

Sonnet 4.6 (default — current model):
  - Building new steps (state machines, complex logic)
  - Debugging architecture-level failures
  - Any step marked "hardest" or with mandatory regression tests
  - Steps 9, 10, 11, 12 specifically

Haiku (via Agent tool with model="haiku"):
  - Codebase exploration, file search, reading existing code
  - Simple one-file edits already designed by Sonnet
  - Running and interpreting test output

Direct Bash (no subagent):
  - Git operations, test runs, file moves

Step classification:
  Step 9  ExitEngine          → Sonnet (hardest, 4 mandatory tests)
  Step 10 DeepSeekPitBoss     → Sonnet (LLM integration, audit logic)
  Step 11 SymbolContext       → Sonnet (multi-symbol state isolation)
  Step 12 CorrelationGuard    → Haiku (math is simple, pattern is clear)
  Step 13 Paper trading       → Haiku (config + wiring only)

════════════════════════════════════════════════════════════════
GIT RULES
════════════════════════════════════════════════════════════════

- Always set git identity before committing:
  git config user.name "DevSiddh"
  git config user.email "challayagneshsaisiddhardha@gmail.com"
- Never commit as Claude
- Always git push origin main after every commit

════════════════════════════════════════════════════════════════
SESSION NOTES
════════════════════════════════════════════════════════════════

- Repo is PRIVATE on GitHub ✅
- MCPs added: context7, code-review ✅
- Hooks added: PostToolUse auto-test, PreToolUse safety guard, Notifications ✅
- .claude/settings.json created ✅
