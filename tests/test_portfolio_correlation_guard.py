"""GP Step 12 — PortfolioCorrelationGuard tests (14 tests).

Covers:
  - No open positions → always allow
  - Insufficient data for proposed symbol → allow (don't block on noise)
  - Insufficient data for existing symbol → skip that pair, don't block
  - High correlation + same direction → BLOCK
  - High correlation + opposite direction → allow (hedge, not amplification)
  - Low correlation + same direction → allow
  - Negative correlation + same direction → allow
  - Same symbol in open_positions → skip self-comparison
  - First breaching pair triggers block (stops checking remaining symbols)
  - update_returns rolling window honours maxlen=50
  - _pearson: identical series → 1.0
  - _pearson: opposite signs → -1.0
  - _pearson: flat/zero series → 0.0 (no ZeroDivisionError)
  - get_correlation_matrix structure and diagonal
"""
import pytest

from portfolio_correlation_guard import (
    CORRELATION_WINDOW,
    MIN_SAMPLES,
    CorrelationResult,
    PortfolioCorrelationGuard,
    _pearson,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _guard(threshold: float = 0.75) -> PortfolioCorrelationGuard:
    return PortfolioCorrelationGuard(threshold=threshold)


def _feed(guard: PortfolioCorrelationGuard, symbol: str, returns: list[float]) -> None:
    """Feed a list of returns to the guard for one symbol."""
    for r in returns:
        guard.update_returns(symbol, r)


def _correlated_series(n: int, target_r: float = 0.95, scale: float = 0.001):
    """Two return series with expected Pearson r ≈ target_r.

    Uses the standard linear mixing formula:
        b[i] = r * common[i] + sqrt(1 - r²) * specific[i]
    so Pearson(a, b) converges to target_r as n grows.
    """
    import random
    import math
    rng = random.Random(42)
    common   = [rng.gauss(0, 1) for _ in range(n)]
    specific = [rng.gauss(0, 1) for _ in range(n)]
    r = target_r
    mix = math.sqrt(max(0.0, 1 - r * r))
    a = [v * scale for v in common]
    b = [(r * common[i] + mix * specific[i]) * scale for i in range(n)]
    return a, b


def _uncorrelated_series(n: int, scale: float = 0.001):
    """Two independent return series (r ≈ 0)."""
    import random
    rng = random.Random(99)
    a = [rng.gauss(0, scale) for _ in range(n)]
    b = [rng.gauss(0, scale) for _ in range(n)]
    return a, b


def _negatively_correlated_series(n: int, target_r: float = -0.95, scale: float = 0.001):
    """Two negatively-correlated return series (r ≈ target_r)."""
    a, b_pos = _correlated_series(n, target_r=abs(target_r), scale=scale)
    b = [-v for v in b_pos]   # flip sign → negative correlation
    return a, b


# ---------------------------------------------------------------------------
# _pearson unit tests
# ---------------------------------------------------------------------------

def test_pearson_identical_series():
    series = [0.001, -0.002, 0.003, 0.0, -0.001] * 4
    assert _pearson(series, series) == pytest.approx(1.0, abs=1e-9)


def test_pearson_opposite_series():
    a = [0.001, -0.002, 0.003, 0.0, -0.001] * 4
    b = [-v for v in a]
    assert _pearson(a, b) == pytest.approx(-1.0, abs=1e-9)


def test_pearson_flat_series_returns_zero():
    """Flat (constant) series → denominator = 0 → return 0 without crash."""
    flat = [0.0] * 30
    assert _pearson(flat, flat) == 0.0


def test_pearson_single_element_returns_zero():
    assert _pearson([0.001], [0.001]) == 0.0


def test_pearson_high_correlation_above_threshold():
    # Use n=100 for stable convergence; assert > 0.85 (well above 0.75 block threshold)
    n = 100
    a, b = _correlated_series(n, target_r=0.95)
    rho = _pearson(a, b)
    assert rho > 0.85, f"Expected high correlation, got {rho:.4f}"


def test_pearson_uncorrelated_near_zero():
    n = 50
    a, b = _uncorrelated_series(n)
    rho = abs(_pearson(a, b))
    assert rho < 0.50, f"Expected near-zero correlation, got {rho:.4f}"


# ---------------------------------------------------------------------------
# PortfolioCorrelationGuard — update_returns / rolling window
# ---------------------------------------------------------------------------

def test_update_returns_rolling_window_caps_at_max():
    guard = _guard()
    for i in range(CORRELATION_WINDOW + 20):
        guard.update_returns("BTC/USDT", float(i) * 0.001)
    assert guard.returns_length("BTC/USDT") == CORRELATION_WINDOW


def test_update_returns_tracks_multiple_symbols_independently():
    guard = _guard()
    _feed(guard, "BTC/USDT", [0.001] * 25)
    _feed(guard, "ETH/USDT", [0.002] * 10)
    assert guard.returns_length("BTC/USDT") == 25
    assert guard.returns_length("ETH/USDT") == 10


# ---------------------------------------------------------------------------
# check() — allow cases
# ---------------------------------------------------------------------------

def test_no_open_positions_always_allows():
    guard = _guard()
    _feed(guard, "BTC/USDT", [0.001] * MIN_SAMPLES)
    result = guard.check("BTC/USDT", "BUY", open_positions={})
    assert result.blocked is False


def test_insufficient_proposed_symbol_data_allows():
    """Proposed symbol has fewer than MIN_SAMPLES → skip check, allow."""
    guard = _guard()
    _feed(guard, "BTC/USDT", [0.001] * (MIN_SAMPLES - 1))
    _feed(guard, "ETH/USDT", [0.001] * MIN_SAMPLES)
    result = guard.check("BTC/USDT", "BUY", open_positions={"ETH/USDT": "BUY"})
    assert result.blocked is False


def test_insufficient_existing_symbol_data_skipped():
    """Existing position symbol has fewer than MIN_SAMPLES → skip that pair."""
    guard = _guard()
    btc, _ = _correlated_series(MIN_SAMPLES)
    _feed(guard, "BTC/USDT", btc)
    _feed(guard, "ETH/USDT", [0.001] * (MIN_SAMPLES - 5))  # insufficient
    result = guard.check("BTC/USDT", "BUY", open_positions={"ETH/USDT": "BUY"})
    assert result.blocked is False


def test_high_correlation_opposite_directions_allows():
    """High correlation but opposite directions (hedge) → allow."""
    guard = _guard(threshold=0.75)
    n = MIN_SAMPLES + 5
    btc, eth = _correlated_series(n)
    _feed(guard, "BTC/USDT", btc)
    _feed(guard, "ETH/USDT", eth)
    # BTC wants to BUY, ETH is already SELL → not same direction
    result = guard.check("BTC/USDT", "BUY", open_positions={"ETH/USDT": "SELL"})
    assert result.blocked is False


def test_low_correlation_same_direction_allows():
    """r well below threshold → allow even with same direction."""
    guard = _guard(threshold=0.75)
    n = MIN_SAMPLES + 10
    btc, eth = _uncorrelated_series(n)
    _feed(guard, "BTC/USDT", btc)
    _feed(guard, "ETH/USDT", eth)
    result = guard.check("BTC/USDT", "BUY", open_positions={"ETH/USDT": "BUY"})
    assert result.blocked is False


def test_negative_correlation_same_direction_allows():
    """Negative correlation → returns move opposite, same direction = hedge."""
    guard = _guard(threshold=0.75)
    n = MIN_SAMPLES + 5
    btc, eth = _negatively_correlated_series(n)
    _feed(guard, "BTC/USDT", btc)
    _feed(guard, "ETH/USDT", eth)
    result = guard.check("BTC/USDT", "BUY", open_positions={"ETH/USDT": "BUY"})
    assert result.blocked is False


def test_same_symbol_in_open_positions_is_skipped():
    """Don't compare proposed_symbol with itself."""
    guard = _guard()
    n = MIN_SAMPLES + 5
    series, _ = _correlated_series(n)
    _feed(guard, "BTC/USDT", series)
    # BTC is already in open_positions (e.g. existing partial fill)
    result = guard.check("BTC/USDT", "BUY", open_positions={"BTC/USDT": "BUY"})
    assert result.blocked is False


# ---------------------------------------------------------------------------
# check() — block cases
# ---------------------------------------------------------------------------

def test_high_correlation_same_direction_blocks():
    """r > threshold AND same direction → BLOCKED."""
    guard = _guard(threshold=0.75)
    n = MIN_SAMPLES + 10
    btc, eth = _correlated_series(n)
    _feed(guard, "BTC/USDT", btc)
    _feed(guard, "ETH/USDT", eth)
    result = guard.check("BTC/USDT", "BUY", open_positions={"ETH/USDT": "BUY"})
    assert result.blocked is True
    assert result.blocking_symbol == "ETH/USDT"
    assert result.correlation > 0.75


def test_block_result_has_required_fields():
    guard = _guard(threshold=0.75)
    n = MIN_SAMPLES + 10
    btc, eth = _correlated_series(n)
    _feed(guard, "BTC/USDT", btc)
    _feed(guard, "ETH/USDT", eth)
    result = guard.check("BTC/USDT", "BUY", open_positions={"ETH/USDT": "BUY"})
    assert isinstance(result, CorrelationResult)
    assert result.blocked is True
    assert result.blocking_symbol is not None
    assert result.correlation > 0.0
    assert result.reason != ""


def test_first_breaching_pair_stops_check():
    """When multiple symbols breach, the first one triggers the block."""
    guard = _guard(threshold=0.75)
    n = MIN_SAMPLES + 10
    btc, eth  = _correlated_series(n)
    btc2, sol = _correlated_series(n, target_r=0.95, scale=0.0015)

    _feed(guard, "BTC/USDT", btc)
    _feed(guard, "ETH/USDT", eth)
    _feed(guard, "SOL/USDT", sol)

    result = guard.check(
        "BTC/USDT", "BUY",
        open_positions={"ETH/USDT": "BUY", "SOL/USDT": "BUY"},
    )
    assert result.blocked is True
    # blocking_symbol must be one of the two existing correlated symbols
    assert result.blocking_symbol in ("ETH/USDT", "SOL/USDT")


def test_sell_direction_blocked_when_correlated():
    """Rule applies equally to SELL positions."""
    guard = _guard(threshold=0.75)
    n = MIN_SAMPLES + 10
    btc, eth = _correlated_series(n)
    _feed(guard, "BTC/USDT", btc)
    _feed(guard, "ETH/USDT", eth)
    result = guard.check("BTC/USDT", "SELL", open_positions={"ETH/USDT": "SELL"})
    assert result.blocked is True


# ---------------------------------------------------------------------------
# get_correlation_matrix
# ---------------------------------------------------------------------------

def test_get_correlation_matrix_diagonal_is_one():
    guard = _guard()
    n = MIN_SAMPLES + 5
    a, b = _correlated_series(n)
    _feed(guard, "BTC/USDT", a)
    _feed(guard, "ETH/USDT", b)
    matrix = guard.get_correlation_matrix()
    assert matrix["BTC/USDT"]["BTC/USDT"] == pytest.approx(1.0)
    assert matrix["ETH/USDT"]["ETH/USDT"] == pytest.approx(1.0)


def test_get_correlation_matrix_excludes_insufficient_data():
    guard = _guard()
    n = MIN_SAMPLES + 5
    a, b = _correlated_series(n)
    _feed(guard, "BTC/USDT", a)
    _feed(guard, "ETH/USDT", b)
    # SOL has fewer than MIN_SAMPLES
    _feed(guard, "SOL/USDT", [0.001] * (MIN_SAMPLES - 1))
    matrix = guard.get_correlation_matrix()
    assert "SOL/USDT" not in matrix


def test_get_correlation_matrix_symmetry():
    guard = _guard()
    n = MIN_SAMPLES + 5
    a, b = _correlated_series(n)
    _feed(guard, "BTC/USDT", a)
    _feed(guard, "ETH/USDT", b)
    matrix = guard.get_correlation_matrix()
    assert matrix["BTC/USDT"]["ETH/USDT"] == pytest.approx(
        matrix["ETH/USDT"]["BTC/USDT"], abs=1e-9
    )


# ---------------------------------------------------------------------------
# Threshold property and summary
# ---------------------------------------------------------------------------

def test_custom_threshold_stored():
    guard = PortfolioCorrelationGuard(threshold=0.80)
    assert guard.threshold == pytest.approx(0.80)


def test_summary_contains_expected_keys():
    guard = _guard()
    _feed(guard, "BTC/USDT", [0.001] * 10)
    s = guard.summary()
    assert "threshold" in s
    assert "tracked_symbols" in s
    assert "returns_lengths" in s
    assert "BTC/USDT" in s["returns_lengths"]
