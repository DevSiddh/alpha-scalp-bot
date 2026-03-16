# Grand Prix Alpha-Scalp — CLAUDE.md

## Project Overview
Binance BTC/USDT scalping bot. Architecture finalized March 16, 2026.
Python 3.12. Status: BUILD PHASE — Steps 1–6 complete.

## Common Commands
pytest tests/ -v
python main.py --paper
python main.py --live

## Architecture Rules — NEVER VIOLATE
- WEIGHTS_LOCKED=true at all times
- WeightOptimizer is REMOVED permanently
- Phase 1 signals only until 200 live trades: bb_squeeze, vwap_cross, liquidity_sweep, trade_aggression, ob_imbalance, mtf_bias, funding_bias
- Build steps are ordered. Do not skip or reorder.
- ExitEngine cannot go live until all 4 regression tests pass.

## 13-Step Build Sequence
Step 1: Freeze weights.json, remove WeightOptimizer — DONE
Step 2: Risk Engine (Three-Strike, Equity Floor, Cash Mode, Kelly) — DONE
Step 3: Quick Wins (spike filter, spread guard, ATR min config, max signal weight validation) — DONE
Step 4: Signal Registry — signal_registry.py [CRITICAL foundation] — DONE
Step 5: SubStrategyManager (5 strategies + Cash) — DONE
Step 6: ShadowTracker (Beta distributions, ghost trades) — DONE — FIX-1, FIX-3
Step 7: TournamentEngine (Thompson Sampling) — 4 hrs
Step 8: StrategyRouter (promote/bench logic) — 4 hrs — FIX-2
Step 9: ExitEngine + 4 regression tests — 8 hrs — FIX-4
Step 10: DeepSeek Pit Boss (audit-only, Sunday memo) — 3 hrs
Step 11: SymbolContext (multi-symbol) — 5 hrs — FIX-10
Step 12: Portfolio Correlation Guard — 2 hrs
Step 13: Paper trading 2 weeks minimum — FIX-9

## Critical Fixes
FIX-1: ShadowTracker div-by-zero — if pnl_max == pnl_min: reward = 0.0
FIX-2: Burn-in — BURN_IN_CANDLES=50 AND distinct_regimes_seen >= 2
FIX-3: Shadow PnL fees — fee = (entry*0.001)+(exit*0.001)
FIX-4: ExitEngine 4 tests — test_ranging_exit_hits_fixed_tp, test_trending_trail_tightens, test_volatile_time_exit, test_breakeven_transition
FIX-9: Paper slippage — Market +0.6xspread, Limit +0.1xspread, VOLATILE x2.0
FIX-10: All-NEUTRAL fallback — global cash mode

## Bug Fixing Protocol
1. When I report a bug, write a failing test first
2. Fix only after test exists
3. Fix is done when test passes AND full suite passes
4. Never delete existing tests

## Current Build Step
CURRENT STEP: 7 — TournamentEngine (Thompson Sampling)
