"""Tests for HypothesisTracker (Step 10 / Part B)."""
from __future__ import annotations

import json
import pytest
import pytest_asyncio

from hypothesis_tracker import (
    Hypothesis,
    HypothesisTracker,
    MIN_SHADOW_TRADES,
    MIN_LOSS_BLOCK_RATE,
    MAX_WIN_BLOCK_RATE,
    BORDERLINE_LOW,
    BORDERLINE_HIGH,
    SOFT_BLOCK_THRESHOLD,
    SHADOW_EXTEND_TRADES,
    META_MIN_ACCURACY,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tracker(tmp_path, llm_caller=None):
    """Return a fresh HypothesisTracker backed by tmp_path files."""
    return HypothesisTracker(
        llm_caller=llm_caller,
        hypotheses_path=tmp_path / "hyp.jsonl",
        registry_path=tmp_path / "registry.json",
    )


def _simulate_trades(tracker, hypothesis_id, *, n_losses, n_wins,
                     loss_blocked_frac=0.80, win_blocked_frac=0.10,
                     regimes=("TRENDING_UP", "RANGING")):
    """Drive on_trade_close so the hypothesis accumulates stats."""
    h = tracker._active[hypothesis_id]
    pattern_key = h.pattern_key

    regime_cycle = list(regimes)
    regime_idx = 0

    for i in range(n_losses):
        blocked = i < int(n_losses * loss_blocked_frac)
        keys = {pattern_key} if blocked else set()
        regime = regime_cycle[regime_idx % len(regime_cycle)]
        regime_idx += 1
        # Directly update to avoid triggering evaluation mid-loop
        h.total_losses_seen += 1
        if blocked:
            h.losses_blocked += 1
            if regime not in h.regimes_effective:
                h.regimes_effective.append(regime)
        h.shadow_trades_completed += 1

    for i in range(n_wins):
        blocked = i < int(n_wins * win_blocked_frac)
        h.total_wins_seen += 1
        if blocked:
            h.wins_blocked += 1
        h.shadow_trades_completed += 1


# ---------------------------------------------------------------------------
# Basic creation
# ---------------------------------------------------------------------------

def test_add_hypothesis_creates_shadow_testing(tmp_path):
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis(
        pattern_key="bearish_fvg_overhead",
        rule_description="Block entries when bearish FVG is overhead",
    )
    assert h.status == "SHADOW_TESTING"
    assert h.hypothesis_id.startswith("H")
    assert h.pattern_key == "bearish_fvg_overhead"
    assert h.shadow_trades_completed == 0


def test_hypothesis_persisted_to_file(tmp_path):
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("test_pattern", "Test description")
    lines = (tmp_path / "hyp.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["hypothesis_id"] == h.hypothesis_id


def test_get_active_hypotheses_returns_all(tmp_path):
    tracker = make_tracker(tmp_path)
    tracker.add_hypothesis("pattern_a", "desc a")
    tracker.add_hypothesis("pattern_b", "desc b")
    assert len(tracker.get_active_hypotheses()) == 2


# ---------------------------------------------------------------------------
# on_trade_close — counter logic
# ---------------------------------------------------------------------------

def test_on_trade_close_loss_increments_correctly(tmp_path):
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("high_vol_entry", "block high vol")

    # Loss with matching pattern — should increment losses_blocked
    tracker.on_trade_close(
        pattern_keys={"high_vol_entry"},
        regime="VOLATILE",
        is_win=False,
    )
    assert h.total_losses_seen == 1
    assert h.losses_blocked == 1
    assert "VOLATILE" in h.regimes_effective


def test_on_trade_close_win_increments_correctly(tmp_path):
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("high_vol_entry", "block high vol")

    tracker.on_trade_close(
        pattern_keys={"high_vol_entry"},
        regime="TRENDING_UP",
        is_win=True,
    )
    assert h.total_wins_seen == 1
    assert h.wins_blocked == 1
    assert h.total_losses_seen == 0


def test_on_trade_close_non_matching_pattern_not_blocked(tmp_path):
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("pattern_x", "desc x")

    tracker.on_trade_close(
        pattern_keys={"different_pattern"},
        regime="RANGING",
        is_win=False,
    )
    assert h.losses_blocked == 0
    assert h.wins_blocked == 0
    assert h.total_losses_seen == 1


# ---------------------------------------------------------------------------
# Auto-approve path (edge > 3.5 → HARD_BLOCK)
# ---------------------------------------------------------------------------

def test_auto_approve_high_edge_ratio(tmp_path):
    """Edge > 3.5 → APPROVED without LLM, tier = HARD_BLOCK."""
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("counter_trend", "block counter-trend")

    # lbr=0.80, wbr=0.05 → edge=16 >> 3.5
    _simulate_trades(tracker, h.hypothesis_id,
                     n_losses=120, n_wins=30,
                     loss_blocked_frac=0.80, win_blocked_frac=0.05,
                     regimes=("TRENDING_UP", "RANGING"))

    # Now fire evaluate through on_trade_close (enough to cross threshold)
    tracker._evaluate_sync(h)

    assert h.status == "APPROVED", f"Expected APPROVED, got {h.status}"
    assert h.confidence_tier == "HARD_BLOCK"
    assert h.approved_at is not None


def test_confidence_tier_soft_block_when_edge_2_5_to_3_5(tmp_path):
    """Edge in 2.5–3.5 range → SOFT_BLOCK tier on approval."""
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("marginal_pattern", "marginal block")

    # lbr=0.70, wbr=0.25 → edge = 2.8 → between BORDERLINE_HIGH(3.5) and MIN_EDGE(2.5)
    # Actually edge < BORDERLINE_LOW is rejected, so we need edge >=3.5 to auto-approve
    # SOFT_BLOCK fires only when edge is >= MIN_EDGE_RATIO but < SOFT_BLOCK_THRESHOLD
    # That only happens via LLM approval path. Let's test it directly via _approve():
    h.edge_ratio = 3.0  # between 2.5 and 3.5
    h.total_losses_seen = 160
    h.total_wins_seen = 40
    h.losses_blocked = 112   # lbr ≈ 0.70
    h.wins_blocked = 8       # wbr = 0.20
    h.regimes_effective = ["TRENDING_UP", "RANGING"]
    h.shadow_trades_completed = 200

    tracker._approve(h)

    assert h.status == "APPROVED"
    assert h.confidence_tier == "SOFT_BLOCK"


# ---------------------------------------------------------------------------
# Auto-reject paths
# ---------------------------------------------------------------------------

def test_reject_insufficient_sample(tmp_path):
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("small_sample", "not enough data")
    # Only 100 trades total
    h.total_losses_seen = 60
    h.total_wins_seen = 40
    h.shadow_trades_completed = 100

    tracker._evaluate_sync(h)
    assert h.status == "REJECTED"


def test_reject_weak_loss_filter(tmp_path):
    """lbr < 0.65 → REJECTED."""
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("weak_pattern", "desc")
    h.total_losses_seen = 100
    h.total_wins_seen = 60
    h.losses_blocked = 60   # lbr = 0.60 < 0.65
    h.wins_blocked = 5
    h.regimes_effective = ["TRENDING_UP", "RANGING"]
    h.shadow_trades_completed = 160

    tracker._evaluate_sync(h)
    assert h.status == "REJECTED"


def test_reject_blocks_too_many_wins(tmp_path):
    """wbr > 0.20 → REJECTED."""
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("overfit_pattern", "desc")
    h.total_losses_seen = 100
    h.total_wins_seen = 60
    h.losses_blocked = 75   # lbr = 0.75 ✓
    h.wins_blocked = 15     # wbr = 0.25 > 0.20 ✗
    h.regimes_effective = ["TRENDING_UP", "RANGING"]
    h.shadow_trades_completed = 160

    tracker._evaluate_sync(h)
    assert h.status == "REJECTED"


def test_reject_single_regime_overfit(tmp_path):
    """Seen in only 1 regime → REJECTED."""
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("regime_specific", "desc")
    h.total_losses_seen = 100
    h.total_wins_seen = 60
    h.losses_blocked = 75   # lbr = 0.75 ✓
    h.wins_blocked = 6      # wbr = 0.10 ✓
    h.regimes_effective = ["TRENDING_UP"]  # only 1 regime ✗
    h.shadow_trades_completed = 160

    tracker._evaluate_sync(h)
    assert h.status == "REJECTED"


def test_reject_edge_below_borderline_low(tmp_path):
    """Edge < 2.3 → REJECTED immediately (no LLM)."""
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("low_edge_pattern", "desc")
    h.total_losses_seen = 100
    h.total_wins_seen = 60
    h.losses_blocked = 70   # lbr = 0.70 ✓
    h.wins_blocked = 18     # wbr = 0.30 → edge = 70/30 = 2.33... wait, wbr = 18/60 = 0.30
    # edge = 0.70 / 0.30 = 2.33 which is in borderline zone (2.3-3.5)
    # Let's make wbr bigger to get edge < 2.3
    h.wins_blocked = 22     # wbr = 22/60 = 0.367 → edge = 0.70/0.367 ≈ 1.91 < 2.3
    h.regimes_effective = ["TRENDING_UP", "RANGING"]
    h.shadow_trades_completed = 160

    tracker._evaluate_sync(h)
    assert h.status == "REJECTED"


def test_reject_borderline_no_llm(tmp_path):
    """Edge 2.3–3.5 with no LLM caller → auto-rejected."""
    tracker = make_tracker(tmp_path, llm_caller=None)
    h = tracker.add_hypothesis("borderline_pattern", "desc")
    h.total_losses_seen = 100
    h.total_wins_seen = 60
    h.losses_blocked = 70   # lbr = 0.70
    h.wins_blocked = 12     # wbr = 12/60 = 0.20 → edge = 0.70/0.20 = 3.5 — exactly BORDERLINE_HIGH
    # Need edge in (2.3, 3.5) — let's use wbr=0.25 → edge=2.8
    h.wins_blocked = 15     # wbr = 0.25 → edge = 0.70/0.25 = 2.80
    h.regimes_effective = ["TRENDING_UP", "RANGING"]
    h.shadow_trades_completed = 160

    tracker._evaluate_sync(h)
    assert h.status == "REJECTED"


# ---------------------------------------------------------------------------
# Borderline + LLM path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_borderline_llm_genuine_approves(tmp_path):
    async def fake_llm(hypothesis, stats):
        return "GENUINE"

    tracker = make_tracker(tmp_path, llm_caller=fake_llm)
    h = tracker.add_hypothesis("borderline_pattern", "desc")
    # lbr=68/100=0.68 ✓ (≥0.65), wbr=12/60=0.20 ✓ (≤0.20), edge=3.4 → borderline (2.3–3.5)
    h.total_losses_seen = 100
    h.total_wins_seen = 60
    h.losses_blocked = 68   # lbr = 0.68
    h.wins_blocked = 12     # wbr = 0.20, edge = 0.68/0.20 = 3.4 < 3.5 → borderline
    h.regimes_effective = ["TRENDING_UP", "RANGING"]
    h.shadow_trades_completed = 160

    tracker._evaluate_sync(h)
    assert h.status == "PENDING_LLM"

    await tracker.evaluate_borderline_async(h)
    assert h.status == "APPROVED"


@pytest.mark.asyncio
async def test_borderline_llm_noise_rejects(tmp_path):
    async def fake_llm(hypothesis, stats):
        return "NOISE"

    tracker = make_tracker(tmp_path, llm_caller=fake_llm)
    h = tracker.add_hypothesis("noise_pattern", "desc")
    h.status = "PENDING_LLM"
    h.edge_ratio = 2.80

    await tracker.evaluate_borderline_async(h)
    assert h.status == "REJECTED"


@pytest.mark.asyncio
async def test_borderline_llm_uncertain_extends_shadow(tmp_path):
    async def fake_llm(hypothesis, stats):
        return "UNCERTAIN"

    tracker = make_tracker(tmp_path, llm_caller=fake_llm)
    h = tracker.add_hypothesis("uncertain_pattern", "desc")
    original_target = h.target_shadow_trades
    h.status = "PENDING_LLM"
    h.edge_ratio = 2.80

    await tracker.evaluate_borderline_async(h)
    assert h.status == "SHADOW_TESTING"
    assert h.target_shadow_trades == original_target + SHADOW_EXTEND_TRADES


# ---------------------------------------------------------------------------
# Meta-validator
# ---------------------------------------------------------------------------

def test_meta_validate_suspends_when_accuracy_below_threshold(tmp_path):
    tracker = make_tracker(tmp_path)

    # 10 blocked trades, only 3 were actual losses → accuracy 30% < 55%
    recent_trades = [
        {"blocked_by": "H123456", "is_win": False} for _ in range(3)
    ] + [
        {"blocked_by": "H123456", "is_win": True} for _ in range(7)
    ]

    # Add a dummy approved hypothesis
    h = tracker.add_hypothesis("test", "desc")
    h.status = "APPROVED"

    result = tracker.meta_validate(recent_trades)
    assert result is False
    assert tracker.system_suspended is True


def test_meta_validate_healthy_system_not_suspended(tmp_path):
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("test", "desc")
    h.status = "APPROVED"

    # 8 correct blocks out of 10 → 80% > 55%
    recent_trades = [
        {"blocked_by": "H123456", "is_win": False} for _ in range(8)
    ] + [
        {"blocked_by": "H123456", "is_win": True} for _ in range(2)
    ]

    result = tracker.meta_validate(recent_trades)
    assert result is True
    assert tracker.system_suspended is False


def test_meta_validate_skips_if_no_approved(tmp_path):
    tracker = make_tracker(tmp_path)
    # No approved hypotheses → should return True (healthy)
    result = tracker.meta_validate([{"blocked_by": "H123", "is_win": True}] * 20)
    assert result is True


def test_meta_validate_skips_if_insufficient_blocked_trades(tmp_path):
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("test", "desc")
    h.status = "APPROVED"

    # Only 9 blocked trades (< 10 threshold) → skip, return True
    recent_trades = [
        {"blocked_by": "H123456", "is_win": True} for _ in range(9)
    ]
    result = tracker.meta_validate(recent_trades)
    assert result is True


# ---------------------------------------------------------------------------
# get_approved
# ---------------------------------------------------------------------------

def test_get_approved_returns_only_approved(tmp_path):
    tracker = make_tracker(tmp_path)
    h1 = tracker.add_hypothesis("p1", "d1")
    h2 = tracker.add_hypothesis("p2", "d2")
    h1.status = "APPROVED"
    # h2 stays SHADOW_TESTING

    approved = tracker.get_approved()
    assert len(approved) == 1
    assert approved[0].pattern_key == "p1"


# ---------------------------------------------------------------------------
# Registry write
# ---------------------------------------------------------------------------

def test_write_registry_produces_valid_json(tmp_path):
    tracker = make_tracker(tmp_path)
    h = tracker.add_hypothesis("reg_test", "registry test")
    h.edge_ratio = 5.0
    h.approved_at = "2026-03-17T00:00:00+00:00"
    tracker._approve(h)

    registry = json.loads((tmp_path / "registry.json").read_text())
    assert len(registry) == 1
    assert registry[0]["pattern_key"] == "reg_test"
    assert registry[0]["active"] is True
