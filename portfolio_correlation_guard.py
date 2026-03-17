"""Alpha-Scalp Bot – PortfolioCorrelationGuard.

GP-S12: Final risk gate — blocks new trades that would create
dangerously correlated cross-symbol exposure.

SHARED by design: one instance for the entire bot.
Must see ALL open positions across ALL symbols simultaneously.

Rule (from CLAUDE.md):
  Block if Pearson r(proposed_symbol, existing_symbol) > THRESHOLD
  AND proposed_direction == existing_position_direction.

Rationale: opening two positively-correlated BUY positions doubles
drawdown when the shared macro factor (BTC) reverses, because both
legs move together. Negative correlation or opposite directions are
fine — they hedge rather than amplify.

Returns: per-candle close-to-close percentage change.
  candle_return = (close - prev_close) / prev_close

Rolling window: 50 candles (CORRELATION_WINDOW).
Minimum data required before a correlation check is trusted: 20 candles
  (MIN_SAMPLES). Below this, the check is skipped (not blocked) —
  false positives from noisy small samples would over-filter early.

Threshold: PORTFOLIO_CORRELATION_THRESHOLD from .env, default 0.75.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

import config as cfg


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CORRELATION_WINDOW: int = 50   # rolling candle window per symbol
MIN_SAMPLES: int = 20          # minimum observations before trusting correlation


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CorrelationResult:
    """Outcome of one portfolio correlation check."""
    blocked: bool                       # True → deny the new trade
    blocking_symbol: str | None = None  # which symbol caused the block
    correlation: float = 0.0            # Pearson r that triggered the block
    reason: str = ""                    # human-readable reason string


# ---------------------------------------------------------------------------
# PortfolioCorrelationGuard
# ---------------------------------------------------------------------------

class PortfolioCorrelationGuard:
    """Blocks new trades that would create correlated cross-symbol exposure.

    Usage
    -----
    guard = PortfolioCorrelationGuard()

    # On every candle close — update all active symbols:
    guard.update_returns("BTC/USDT", 0.0012)   # (close - prev) / prev
    guard.update_returns("ETH/USDT", 0.0009)

    # Before opening a new trade:
    open_positions = {"ETH/USDT": "BUY"}       # from SymbolContextRegistry
    result = guard.check("BTC/USDT", "BUY", open_positions)
    if result.blocked:
        return  # deny entry

    Parameters
    ----------
    threshold:
        Pearson r above which a new trade is blocked when directions match.
        Defaults to PORTFOLIO_CORRELATION_THRESHOLD from config / .env (0.75).
    """

    CORRELATION_WINDOW: int = CORRELATION_WINDOW
    MIN_SAMPLES: int = MIN_SAMPLES

    def __init__(self, threshold: float | None = None) -> None:
        self._threshold: float = (
            threshold
            if threshold is not None
            else float(getattr(cfg, "PORTFOLIO_CORRELATION_THRESHOLD", 0.75))
        )
        # symbol → rolling deque of per-candle returns (maxlen = CORRELATION_WINDOW)
        self._returns: dict[str, deque[float]] = {}

    # ── Return feed ─────────────────────────────────────────────────────────

    def update_returns(self, symbol: str, candle_return: float) -> None:
        """Record one candle's return for a symbol.

        Call once per closed candle for every tracked symbol.
        Typically: candle_return = (close - prev_close) / prev_close

        Parameters
        ----------
        symbol:
            Normalised symbol string, e.g. "BTC/USDT".
        candle_return:
            Fractional return (not percentage). E.g. 0.0012 for +0.12%.
        """
        if symbol not in self._returns:
            self._returns[symbol] = deque(maxlen=self.CORRELATION_WINDOW)
        self._returns[symbol].append(float(candle_return))

    # ── Core check ──────────────────────────────────────────────────────────

    def check(
        self,
        proposed_symbol: str,
        proposed_direction: str,
        open_positions: dict[str, str],
    ) -> CorrelationResult:
        """Check if a proposed new trade would create dangerous correlation.

        Parameters
        ----------
        proposed_symbol:
            The symbol being considered for a new trade, e.g. "BTC/USDT".
        proposed_direction:
            Direction of the proposed trade: "BUY" or "SELL".
        open_positions:
            Snapshot of ALL currently open positions across ALL symbols.
            Format: {symbol: direction}, e.g. {"ETH/USDT": "BUY"}.
            Provided by the caller (main loop / SymbolContextRegistry).
            The proposed_symbol itself should NOT appear in open_positions
            unless there is already an open trade in it.

        Returns
        -------
        CorrelationResult
            blocked=True  → deny the new trade entry.
            blocked=False → allow the trade to proceed.
        """
        if not open_positions:
            return CorrelationResult(blocked=False, reason="no_open_positions")

        proposed_dir = proposed_direction.upper()

        proposed_returns = self._returns.get(proposed_symbol)
        if proposed_returns is None or len(proposed_returns) < self.MIN_SAMPLES:
            logger.debug(
                "PortfolioCorrelationGuard: {} skipped (samples={}/{})",
                proposed_symbol,
                len(proposed_returns) if proposed_returns else 0,
                self.MIN_SAMPLES,
            )
            return CorrelationResult(
                blocked=False,
                reason=f"insufficient_data_{proposed_symbol}",
            )

        for existing_symbol, existing_dir in open_positions.items():
            # Skip self-comparison
            if existing_symbol == proposed_symbol:
                continue

            # Only block when directions match — opposite directions hedge
            if existing_dir.upper() != proposed_dir:
                continue

            existing_returns = self._returns.get(existing_symbol)
            if existing_returns is None or len(existing_returns) < self.MIN_SAMPLES:
                continue   # not enough data for this symbol — skip, don't block

            # Align to the shorter available window for a fair comparison
            n = min(len(proposed_returns), len(existing_returns))
            r_prop  = list(proposed_returns)[-n:]
            r_exist = list(existing_returns)[-n:]

            rho = _pearson(r_prop, r_exist)

            logger.debug(
                "PortfolioCorrelationGuard: {} {} vs {} {} | rho={:.4f} threshold={}",
                proposed_symbol, proposed_dir,
                existing_symbol, existing_dir,
                rho, self._threshold,
            )

            if rho > self._threshold:
                logger.warning(
                    "PortfolioCorrelationGuard: BLOCKED {} {} — "
                    "correlated with {} {} (rho={:.3f} > {:.3f})",
                    proposed_symbol, proposed_dir,
                    existing_symbol, existing_dir,
                    rho, self._threshold,
                )
                return CorrelationResult(
                    blocked=True,
                    blocking_symbol=existing_symbol,
                    correlation=round(rho, 4),
                    reason=(
                        f"corr_{proposed_symbol}_vs_{existing_symbol}"
                        f"_rho={rho:.3f}_threshold={self._threshold}"
                    ),
                )

        return CorrelationResult(blocked=False, reason="correlation_ok")

    # ── Introspection ────────────────────────────────────────────────────────

    def get_correlation_matrix(self) -> dict[str, dict[str, float]]:
        """Pairwise Pearson r for all symbols that have ≥ MIN_SAMPLES data.

        Returns a nested dict: matrix[sym_a][sym_b] = rho.
        Diagonal is always 1.0.
        """
        symbols = [
            s for s, r in self._returns.items()
            if len(r) >= self.MIN_SAMPLES
        ]
        matrix: dict[str, dict[str, float]] = {}
        for a in symbols:
            matrix[a] = {}
            for b in symbols:
                if a == b:
                    matrix[a][b] = 1.0
                    continue
                n = min(len(self._returns[a]), len(self._returns[b]))
                ra = list(self._returns[a])[-n:]
                rb = list(self._returns[b])[-n:]
                matrix[a][b] = round(_pearson(ra, rb), 4)
        return matrix

    def returns_length(self, symbol: str) -> int:
        """Number of candle returns currently stored for *symbol*."""
        return len(self._returns.get(symbol, []))

    @property
    def threshold(self) -> float:
        """The Pearson r threshold in use."""
        return self._threshold

    def summary(self) -> dict[str, Any]:
        """Quick state snapshot for logging."""
        return {
            "threshold":      self._threshold,
            "tracked_symbols": list(self._returns.keys()),
            "returns_lengths": {
                sym: len(ret) for sym, ret in self._returns.items()
            },
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pearson(x: list[float], y: list[float]) -> float:
    """Pearson correlation coefficient for two equal-length sequences.

    Returns 0.0 when the denominator is zero (flat / constant series).
    """
    n = len(x)
    if n < 2:
        return 0.0

    mx = sum(x) / n
    my = sum(y) / n

    numerator   = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    denom_sq    = (
        sum((xi - mx) ** 2 for xi in x)
        * sum((yi - my) ** 2 for yi in y)
    )

    if denom_sq <= 0.0:
        return 0.0

    return numerator / denom_sq ** 0.5
