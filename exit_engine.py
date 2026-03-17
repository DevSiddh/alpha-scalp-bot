"""Alpha-Scalp Bot – ExitEngine (GP Step 9).

4-state machine that manages the exit of a single open position.
One instance created on entry, destroyed on exit. Never reused.

States:
  0 ENTRY     — monitor for breakeven trigger
  1 BREAKEVEN — SL moved to entry, monitor trailing
  2 TRAILING  — regime-dependent active trail
  3 EXIT      — position closed, all details logged

Regime behaviour:
  RANGING / NEUTRAL  — fixed TP at entry ± 1.5×ATR, no trail
  TRENDING (any)     — tightening ATR trail: 2× → 1× at +5% → 0.75× at +10%
  VOLATILE           — tight 1×ATR trail if in profit;
                       time-exit if no profit after 4 candles
  TRANSITION         — treated as TRENDING (tightening trail)

Mandatory regression tests (all in tests/test_exit_engine.py):
  test_ranging_exit_hits_fixed_tp_not_trailing()
  test_trending_trail_tightens_at_5pct_profit()
  test_volatile_time_exit_triggers_at_candle_4()
  test_breakeven_state_transitions_correctly()
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# Constants  (source of truth: CLAUDE.md spec — do NOT import from config.py
# which still has legacy values from the pre-Step-9 codebase)
# ---------------------------------------------------------------------------

_TRAIL_ACTIVATE_PCT: float = 0.005      # 0.5%  — breakeven trigger
_TRAIL_ATR_MULT_NORMAL: float = 2.0     # TRENDING initial trail
_TRAIL_ATR_MULT_5PCT: float = 1.0       # TRENDING tighten at +5%
_TRAIL_ATR_MULT_10PCT: float = 0.75     # TRENDING tighten at +10%
_RANGING_TP_ATR_MULT: float = 1.5       # RANGING / NEUTRAL fixed TP
_VOLATILE_TIME_EXIT_CANDLES: int = 4    # VOLATILE — force exit candles


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ExitSignal:
    """Returned by ExitEngine.on_candle() on every candle."""
    action: str                      # "HOLD" | "UPDATE_SL" | "EXIT"
    new_sl: float | None = None      # populated on UPDATE_SL
    exit_price: float | None = None  # populated on EXIT
    exit_reason: str | None = None   # "sl_hit" | "tp_hit" | "volatile_time_exit"
    state: int = 0                   # current state after processing


# ---------------------------------------------------------------------------
# ExitEngine
# ---------------------------------------------------------------------------

class ExitEngine:
    """4-state exit machine for a single open position.

    Parameters
    ----------
    position : dict
        Output of conftest.make_position() or bot_state.json open_positions entry.
        Required keys: position_id, symbol, side, size, entry_price,
        entry_atr, regime_at_entry, sl_price, tp_price.
        Optional keys: strategy, candles_open, exit_state, state_history.
    """

    STATE_ENTRY = 0
    STATE_BREAKEVEN = 1
    STATE_TRAILING = 2
    STATE_EXIT = 3

    def __init__(self, position: dict[str, Any]) -> None:
        self.position_id: str = position["position_id"]
        self.symbol: str = position["symbol"]
        self.side: str = position["side"]        # "BUY" | "SELL"
        self.size: float = position["size"]
        self.entry_price: float = position["entry_price"]
        self.entry_atr: float = position["entry_atr"]
        self.regime_at_entry: str = position["regime_at_entry"]
        self.strategy: str = position.get("strategy", "unknown")

        self.sl_price: float = position["sl_price"]
        self.tp_price: float = position["tp_price"]
        self.state: int = position.get("exit_state", self.STATE_ENTRY)
        self.state_history: list[dict[str, Any]] = list(position.get("state_history", []))
        self.candles_open: int = position.get("candles_open", 0)

        # RANGING / NEUTRAL: override TP with tighter fixed target
        if self._is_ranging():
            self.tp_price = self._ranging_tp()

        # Populated on exit
        self.exit_price: float | None = None
        self.exit_reason: str | None = None
        self.hold_duration: int = 0

        self._created_at: float = time.time()

        logger.debug(
            "ExitEngine | {} {} {} | entry={:.2f} SL={:.2f} TP={:.2f} regime={}",
            self.position_id, self.side, self.symbol,
            self.entry_price, self.sl_price, self.tp_price, self.regime_at_entry,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_candle(self, current_price: float, current_atr: float) -> ExitSignal:
        """Process one closed candle. Call once per candle for an open position.

        Returns ExitSignal.action:
          "HOLD"      — no action needed
          "UPDATE_SL" — send amended SL order, new_sl is the target price
          "EXIT"      — close the position at market; reason in exit_reason
        """
        if self.state == self.STATE_EXIT:
            # Already exited — return cached result (idempotent)
            return ExitSignal(
                action="EXIT",
                exit_price=self.exit_price,
                exit_reason=self.exit_reason,
                state=self.STATE_EXIT,
            )

        self.candles_open += 1

        # ── SL hit (always active, all states, all regimes) ────────────
        if self._sl_hit(current_price):
            return self._trigger_exit(current_price, "sl_hit")

        # ── RANGING / NEUTRAL: fixed TP check (always active) ─────────
        # TP is tighter than breakeven trigger so it fires from State 0
        if self._is_ranging() and self._tp_hit(current_price):
            return self._trigger_exit(current_price, "tp_hit")

        # ── State machine ──────────────────────────────────────────────
        if self.state == self.STATE_ENTRY:
            return self._process_entry(current_price, current_atr)
        if self.state == self.STATE_BREAKEVEN:
            return self._process_breakeven(current_price, current_atr)
        if self.state == self.STATE_TRAILING:
            return self._process_trailing(current_price, current_atr)

        return ExitSignal(action="HOLD", state=self.state)

    def to_dict(self) -> dict[str, Any]:
        """Serialise engine state for bot_state.json persistence."""
        return {
            "position_id":     self.position_id,
            "symbol":          self.symbol,
            "side":            self.side,
            "size":            self.size,
            "entry_price":     self.entry_price,
            "entry_atr":       self.entry_atr,
            "regime_at_entry": self.regime_at_entry,
            "strategy":        self.strategy,
            "sl_price":        self.sl_price,
            "tp_price":        self.tp_price,
            "exit_state":      self.state,
            "state_history":   self.state_history,
            "candles_open":    self.candles_open,
            "exit_price":      self.exit_price,
            "exit_reason":     self.exit_reason,
            "hold_duration":   self.hold_duration,
        }

    # ------------------------------------------------------------------
    # State processors
    # ------------------------------------------------------------------

    def _process_entry(self, price: float, atr: float) -> ExitSignal:
        """State 0 — wait for breakeven trigger or VOLATILE fast-path."""
        # VOLATILE skips breakeven entirely and starts trail logic immediately
        if self._is_volatile():
            self._transition_to(self.STATE_TRAILING, price)
            return self._process_trailing(price, atr)

        if self._breakeven_triggered(price):
            self._transition_to(self.STATE_BREAKEVEN, price)
            self.sl_price = self.entry_price
            logger.info(
                "ExitEngine BREAKEVEN | {} | SL → entry={:.2f}",
                self.position_id, self.entry_price,
            )
            return self._process_breakeven(price, atr)

        return ExitSignal(action="HOLD", state=self.state)

    def _process_breakeven(self, price: float, atr: float) -> ExitSignal:
        """State 1 — SL at entry. RANGING holds fixed TP; others go to trailing."""
        if self._is_ranging():
            # Fixed TP is already checked in on_candle; nothing else to do here
            return ExitSignal(action="HOLD", state=self.state)

        # Non-ranging: advance to active trailing immediately
        self._transition_to(self.STATE_TRAILING, price)
        return self._process_trailing(price, atr)

    def _process_trailing(self, price: float, atr: float) -> ExitSignal:
        """State 2 — regime-dependent trailing logic."""
        if self._is_ranging():
            # Should not normally reach here (TP fires from on_candle),
            # but guard just in case
            if self._tp_hit(price):
                return self._trigger_exit(price, "tp_hit")
            return ExitSignal(action="HOLD", state=self.state)

        if self._is_volatile():
            return self._process_volatile_trailing(price, atr)

        # TRENDING / TRANSITION / NEUTRAL-as-trending
        return self._process_trend_trailing(price, atr)

    def _process_trend_trailing(self, price: float, atr: float) -> ExitSignal:
        """TRENDING: tightening ATR trail at +5% and +10% profit."""
        profit_pct = self._profit_pct(price)

        if profit_pct >= 0.10:
            mult = _TRAIL_ATR_MULT_10PCT   # 0.75×ATR
        elif profit_pct >= 0.05:
            mult = _TRAIL_ATR_MULT_5PCT    # 1.0×ATR
        else:
            mult = _TRAIL_ATR_MULT_NORMAL  # 2.0×ATR

        new_sl = self._trail_sl(price, atr, mult)

        # Ratchet: only move SL in the favourable direction — never loosen
        if self.side == "BUY" and new_sl > self.sl_price:
            self.sl_price = new_sl
            return ExitSignal(action="UPDATE_SL", new_sl=new_sl, state=self.state)
        if self.side == "SELL" and new_sl < self.sl_price:
            self.sl_price = new_sl
            return ExitSignal(action="UPDATE_SL", new_sl=new_sl, state=self.state)

        return ExitSignal(action="HOLD", state=self.state)

    def _process_volatile_trailing(self, price: float, atr: float) -> ExitSignal:
        """VOLATILE: tight 1×ATR trail if in profit; time-exit at candle 4."""
        if self._in_profit(price):
            new_sl = self._trail_sl(price, atr, 1.0)
            if self.side == "BUY" and new_sl > self.sl_price:
                self.sl_price = new_sl
                return ExitSignal(action="UPDATE_SL", new_sl=new_sl, state=self.state)
            if self.side == "SELL" and new_sl < self.sl_price:
                self.sl_price = new_sl
                return ExitSignal(action="UPDATE_SL", new_sl=new_sl, state=self.state)
            return ExitSignal(action="HOLD", state=self.state)

        # No profit — count candles and force-exit at threshold
        if self.candles_open >= _VOLATILE_TIME_EXIT_CANDLES:
            return self._trigger_exit(price, "volatile_time_exit")

        return ExitSignal(action="HOLD", state=self.state)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sl_hit(self, price: float) -> bool:
        if self.side == "BUY":
            return price <= self.sl_price
        return price >= self.sl_price

    def _tp_hit(self, price: float) -> bool:
        if self.side == "BUY":
            return price >= self.tp_price
        return price <= self.tp_price

    def _breakeven_triggered(self, price: float) -> bool:
        if self.side == "BUY":
            return price >= self.entry_price * (1.0 + _TRAIL_ACTIVATE_PCT)
        return price <= self.entry_price * (1.0 - _TRAIL_ACTIVATE_PCT)

    def _in_profit(self, price: float) -> bool:
        if self.side == "BUY":
            return price > self.entry_price
        return price < self.entry_price

    def _profit_pct(self, price: float) -> float:
        """Unrealised profit as a fraction of entry price."""
        if self.side == "BUY":
            return (price - self.entry_price) / self.entry_price
        return (self.entry_price - price) / self.entry_price

    def _trail_sl(self, price: float, atr: float, mult: float) -> float:
        """Compute trailing SL price given current price, ATR and multiplier."""
        if self.side == "BUY":
            return price - mult * atr
        return price + mult * atr

    def _ranging_tp(self) -> float:
        """Fixed TP for RANGING / NEUTRAL: entry ± 1.5×ATR."""
        if self.side == "BUY":
            return self.entry_price + _RANGING_TP_ATR_MULT * self.entry_atr
        return self.entry_price - _RANGING_TP_ATR_MULT * self.entry_atr

    def _is_ranging(self) -> bool:
        return self.regime_at_entry in ("RANGING", "NEUTRAL")

    def _is_volatile(self) -> bool:
        return self.regime_at_entry == "VOLATILE"

    def _transition_to(self, new_state: int, price: float) -> None:
        self.state_history.append({
            "from_state":   self.state,
            "to_state":     new_state,
            "price":        price,
            "candles_open": self.candles_open,
        })
        self.state = new_state
        logger.debug(
            "ExitEngine {} → state {} | {} price={:.2f} candles={}",
            new_state - 1, new_state, self.position_id, price, self.candles_open,
        )

    def _trigger_exit(self, price: float, reason: str) -> ExitSignal:
        self._transition_to(self.STATE_EXIT, price)
        self.exit_price = price
        self.exit_reason = reason
        self.hold_duration = self.candles_open
        logger.info(
            "ExitEngine EXIT | {} | reason={} price={:.2f} candles={} history={}",
            self.position_id, reason, price, self.hold_duration, self.state_history,
        )
        return ExitSignal(
            action="EXIT",
            exit_price=price,
            exit_reason=reason,
            state=self.STATE_EXIT,
        )
